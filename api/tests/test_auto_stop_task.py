"""Tests for the AKS idle auto-stop Celery driver.

Responsibility: Cover the two-task driver — `evaluate_idle_clusters`
    (beat) and `auto_stop_aks` (per-cluster) — without touching Azure
    SDK. The state-repo + preferences + power-state + `stop_aks` calls
    are all stubbed; the driver's only job is to call them in the right
    order.
Edit boundaries: Driver layer only. The decision algorithm lives in
    `auto_stop_evaluator` (and has its own tests there).
Key entry points: see per-test docstrings.
Risky contracts: `auto_stop_aks.run` re-evaluates the decision before
    calling `stop_aks` — that re-check is the safety net against the
    decide-vs-act race. Locked here.
Validation: `uv run pytest -q api/tests/test_auto_stop_task.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.auto_stop import AutoStopPreference, save_auto_stop_preference
from api.services.auto_stop_evaluator import IdleDecision


@pytest.fixture(autouse=True)
def _file_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path, request: pytest.FixtureRequest
) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    # Driver pulls `get_state_repo()` inside the task body — stub it so the
    # task can run without a real `AZURE_TABLE_ENDPOINT`.
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: object()
    )
    # The driver probes live K8s ``app=blast`` activity for Running
    # clusters; stub it to "unavailable" by default so the task tests stay
    # hermetic (no Azure/K8s call). Wiring tests below override this.
    monkeypatch.setattr(
        "api.tasks.azure.idle_autostop._live_blast_signal",
        lambda _pref, _power_state: (None, None),
    )
    # The beat driver resolves cluster power_state via one batched ARM call
    # (`_batch_power_states` → `management.azure.com`). The run()-driver tests
    # mock `evaluate_cluster`, so the power_state value is irrelevant to them;
    # stub the batch probe to "Running" for every pref so they stay hermetic
    # and don't pay a real ARM connect/retry (~2 s, flaky in CI). The two tests
    # that exercise the batch path itself opt out by name.
    if "batch_power_states" not in request.node.name:
        monkeypatch.setattr(
            "api.tasks.azure.idle_autostop._batch_power_states",
            lambda prefs: (
                {
                    (p.subscription_id, p.resource_group, p.cluster_name): "Running"
                    for p in prefs
                },
                {
                    "rg_groups": len(
                        {(p.subscription_id, p.resource_group) for p in prefs}
                    ),
                    "rg_failed": 0,
                    "failed_rgs": [],
                },
            ),
        )


def _pref(**overrides: object) -> AutoStopPreference:
    base = AutoStopPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        enabled=True,
        idle_minutes=60,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_auto_stop_aks_aborts_when_preference_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user disabled auto-stop between beat and task, do nothing."""
    from api.tasks.azure import idle_autostop

    monkeypatch.setattr(
        idle_autostop, "get_auto_stop_preference", lambda *_a, **_kw: None
    )
    stop_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.tasks.azure.stop_aks.run",
        lambda **kwargs: stop_calls.append(kwargs),
    )

    result = idle_autostop.auto_stop_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    assert result["action"] == "skip"
    assert result["reason"] == "preference_missing_or_disabled"
    assert stop_calls == []


def test_auto_stop_aks_re_evaluates_and_skips_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A racing BLAST submit between beat-decide and task-act must abort."""
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(_pref())
    monkeypatch.setattr(
        idle_autostop, "_power_state", lambda _pref: "Running"
    )
    monkeypatch.setattr(idle_autostop, "_provisioning_state", lambda _pref: "Succeeded")
    monkeypatch.setattr(
        idle_autostop,
        "evaluate_cluster",
        lambda pref, *, repo, power_state, ignore_cooldown=False, **_extra: IdleDecision(
            verdict="keep", reason="active_jobs:1", active_job_count=1
        ),
    )
    stop_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.tasks.azure.stop_aks.run",
        lambda **kwargs: stop_calls.append(kwargs),
    )

    result = idle_autostop.auto_stop_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    assert result["action"] == "skip"
    assert result["reason"] == "active_jobs:1"
    assert stop_calls == []


def test_auto_stop_aks_calls_stop_when_evaluator_returns_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path — re-evaluator confirms stop, driver invokes `stop_aks`."""
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(_pref())
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _pref: "Running")
    monkeypatch.setattr(idle_autostop, "_provisioning_state", lambda _pref: "Succeeded")
    monkeypatch.setattr(
        idle_autostop,
        "evaluate_cluster",
        lambda pref, *, repo, power_state, ignore_cooldown=False, **_extra: IdleDecision(
            verdict="stop", reason="idle:60m"
        ),
    )
    stop_calls: list[dict[str, Any]] = []

    def fake_stop_run(**kwargs: Any) -> dict[str, Any]:
        stop_calls.append(kwargs)
        return {"cluster_name": kwargs["cluster_name"], "status": "completed"}

    monkeypatch.setattr("api.tasks.azure.stop_aks.run", fake_stop_run)

    result = idle_autostop.auto_stop_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    assert result["action"] == "stop"
    assert result["reason"] == "idle:60m"
    assert len(stop_calls) == 1
    assert stop_calls[0]["cluster_name"] == "elb-cluster"


def test_auto_stop_aks_stops_despite_preflight_cooldown_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the beat driver stamps ``last_stop_at`` BEFORE enqueueing
    the act task (its double-enqueue guard). The act task uses the REAL
    ``evaluate_cluster`` and MUST still stop the idle cluster — otherwise
    that fresh stamp trips ``is_in_cooldown`` and the act task late-skips
    its own enqueued stop forever (the observed prod livelock where
    ``auto_stop_aks late-skip reason=cooldown`` repeated every cooldown
    window and the cluster never stopped).
    """
    from datetime import UTC, datetime, timedelta

    from api.services.auto_stop import get_auto_stop_preference
    from api.tasks.azure import idle_autostop

    now = datetime.now(UTC)
    save_auto_stop_preference(
        _pref(
            cluster_name="elb-cluster",
            enabled=True,
            cooldown_minutes=30,
            # Preflight stamp the beat just wrote — well inside cooldown.
            last_stop_at=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            # Anchored long ago so the idle window has elapsed.
            created_at=(now - timedelta(hours=4)).isoformat(timespec="seconds"),
        )
    )
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _pref: "Running")
    monkeypatch.setattr(idle_autostop, "_provisioning_state", lambda _pref: "Succeeded")
    # Real evaluate_cluster with an idle (no active jobs) repo.
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: type("_R", (), {"list_for_scope": lambda self, **_kw: []})(),
    )
    stop_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.tasks.azure.stop_aks.run",
        lambda **kwargs: stop_calls.append(kwargs) or {"status": "completed"},
    )

    result = idle_autostop.auto_stop_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    assert result["action"] == "stop", result
    assert len(stop_calls) == 1
    persisted = get_auto_stop_preference("sub-1", "rg-elb", "elb-cluster")
    assert persisted is not None
    assert persisted.last_stop_reason.startswith("idle:")


def test_evaluate_idle_clusters_enqueues_per_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beat task walks every enabled pref and enqueues `auto_stop_aks` for stops."""
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(
        _pref(cluster_name="cluster-stop", enabled=True)
    )
    save_auto_stop_preference(
        _pref(cluster_name="cluster-keep", enabled=True)
    )
    save_auto_stop_preference(
        _pref(cluster_name="cluster-disabled", enabled=False)
    )

    def fake_eval(
        pref: AutoStopPreference, *, repo, power_state: str, **_extra
    ) -> IdleDecision:
        if pref.cluster_name == "cluster-stop":
            return IdleDecision(verdict="stop", reason="idle:60m")
        return IdleDecision(verdict="keep", reason="active")

    monkeypatch.setattr(idle_autostop, "evaluate_cluster", fake_eval)
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _p: "Running")

    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        idle_autostop.auto_stop_aks,
        "delay",
        lambda **kwargs: enqueued.append(kwargs) or object(),
    )

    summary = idle_autostop.evaluate_idle_clusters.run()
    # Only "cluster-stop" should have been enqueued.
    assert summary["evaluated"] == 2  # disabled is skipped before evaluate
    assert summary["queued_stops"] == 1
    assert summary["kept_running"] == 1
    assert len(enqueued) == 1
    assert enqueued[0]["cluster_name"] == "cluster-stop"


def test_evaluate_idle_clusters_warn_marks_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`warn` verdict records a skip note (drives the SPA banner) without enqueueing."""
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(_pref(cluster_name="cluster-warn"))
    monkeypatch.setattr(
        idle_autostop,
        "evaluate_cluster",
        lambda pref, *, repo, power_state, **_extra: IdleDecision(
            verdict="warn", reason="idle_pending"
        ),
    )
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _p: "Running")
    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        idle_autostop.auto_stop_aks,
        "delay",
        lambda **kwargs: enqueued.append(kwargs) or object(),
    )

    summary = idle_autostop.evaluate_idle_clusters.run()
    assert summary["queued_stops"] == 0
    assert summary["warnings"] == 1
    assert enqueued == []


def test_evaluate_idle_clusters_stamps_last_stop_at_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beat must stamp ``last_stop_at`` BEFORE enqueueing so the next
    overlapping beat tick (running before the worker has finished the
    stop) sees ``is_in_cooldown`` and refuses to double-enqueue."""
    from api.services.auto_stop import get_auto_stop_preference
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(_pref(cluster_name="cluster-stop", enabled=True))
    monkeypatch.setattr(
        idle_autostop,
        "evaluate_cluster",
        lambda pref, *, repo, power_state, **_extra: IdleDecision(
            verdict="stop", reason="idle:60m"
        ),
    )
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _p: "Running")
    monkeypatch.setattr(
        idle_autostop.auto_stop_aks, "delay", lambda **_kw: object()
    )
    idle_autostop.evaluate_idle_clusters.run()
    persisted = get_auto_stop_preference("sub-1", "rg-elb", "cluster-stop")
    assert persisted is not None
    assert persisted.last_stop_at != ""
    assert persisted.last_stop_reason.startswith("enqueued:")


def test_evaluate_idle_clusters_warn_writes_only_on_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated `warn` ticks must not re-write the preference each time."""
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(_pref(cluster_name="cluster-warn"))
    monkeypatch.setattr(
        idle_autostop,
        "evaluate_cluster",
        lambda pref, *, repo, power_state, **_extra: IdleDecision(
            verdict="warn", reason="idle_pending"
        ),
    )
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _p: "Running")
    monkeypatch.setattr(
        idle_autostop.auto_stop_aks, "delay", lambda **_kw: object()
    )

    write_count = {"n": 0}
    real_mark = idle_autostop.mark_auto_stop_event

    def counting_mark(pref, *, stopped, reason):
        write_count["n"] += 1
        return real_mark(pref, stopped=stopped, reason=reason)

    monkeypatch.setattr(idle_autostop, "mark_auto_stop_event", counting_mark)

    idle_autostop.evaluate_idle_clusters.run()
    idle_autostop.evaluate_idle_clusters.run()
    idle_autostop.evaluate_idle_clusters.run()
    # Only the FIRST warn tick writes; the subsequent two see
    # `last_skip_reason="warn:..."` and skip the Table write.
    assert write_count["n"] == 1


def test_auto_stop_aks_has_no_autoretry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`auto_stop_aks` MUST NOT retry — the inner `stop_aks` already
    handles transient ARM failures and retrying here multiplies stops."""
    from api.tasks.azure import idle_autostop

    task = idle_autostop.auto_stop_aks
    # Celery exposes retry settings via the task's options/kwargs.
    assert getattr(task, "max_retries", 0) == 0
    assert not getattr(task, "autoretry_for", ())


def test_evaluate_idle_clusters_batches_power_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beat must use the batched power_state path so 100 clusters in
    1 RG don't issue 100 ARM `managed_clusters.get` calls per tick."""
    from api.tasks.azure import idle_autostop

    save_auto_stop_preference(_pref(cluster_name="cluster-a", enabled=True))
    save_auto_stop_preference(_pref(cluster_name="cluster-b", enabled=True))
    save_auto_stop_preference(_pref(cluster_name="cluster-c", enabled=True))

    per_cluster_calls = {"n": 0}
    batch_calls = {"n": 0}

    def fake_per_cluster(_pref):
        per_cluster_calls["n"] += 1
        return "Running"

    def fake_batch(prefs):
        batch_calls["n"] += 1
        states = {
            (p.subscription_id, p.resource_group, p.cluster_name): "Running"
            for p in prefs
        }
        # Critique #15: ``_batch_power_states`` returns ``(states, summary)``
        # so the beat fan-out can attribute ARM failures by (sub, rg).
        summary = {"rg_groups": 0, "rg_failed": 0, "failed_rgs": []}
        return states, summary

    monkeypatch.setattr(idle_autostop, "_power_state", fake_per_cluster)
    monkeypatch.setattr(idle_autostop, "_batch_power_states", fake_batch)
    monkeypatch.setattr(
        idle_autostop,
        "evaluate_cluster",
        lambda pref, *, repo, power_state, **_extra: IdleDecision(
            verdict="keep", reason="active"
        ),
    )

    summary = idle_autostop.evaluate_idle_clusters.run()
    assert summary["evaluated"] == 3
    # The per-cluster ARM helper must NOT be called by the beat fan-out.
    assert per_cluster_calls["n"] == 0
    # The batched helper is called exactly once per tick regardless of
    # cluster count.
    assert batch_calls["n"] == 1


def test_batch_power_states_groups_by_rg(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_batch_power_states` issues one ARM `list_by_resource_group` per
    unique (subscription_id, resource_group) tuple, NOT per cluster."""
    from api.services.auto_stop import AutoStopPreference
    from api.tasks.azure import idle_autostop

    list_calls: list[tuple[str, str]] = []

    class _FakeManagedClusters:
        def __init__(self, sub: str, rg: str) -> None:
            self.sub = sub
            self.rg = rg

        def list_by_resource_group(self, rg: str):
            list_calls.append((self.sub, rg))
            # Return two clusters in this RG; only one matches our prefs.
            from types import SimpleNamespace

            return [
                SimpleNamespace(
                    name="cluster-a",
                    power_state=SimpleNamespace(code="Running"),
                ),
                SimpleNamespace(
                    name="cluster-b",
                    power_state=SimpleNamespace(code="Stopped"),
                ),
            ]

    class _FakeClient:
        def __init__(self, sub: str) -> None:
            self.managed_clusters = _FakeManagedClusters(sub, "")

    def fake_aks_client(_cred, sub: str):
        return _FakeClient(sub)

    monkeypatch.setattr(
        "api.services.azure_clients.aks_client", fake_aks_client
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    prefs = [
        AutoStopPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="cluster-a",
        ),
        AutoStopPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="cluster-b",
        ),
        AutoStopPreference(
            subscription_id="sub-1",
            resource_group="rg-other",
            cluster_name="cluster-c",
        ),
    ]
    states, summary = idle_autostop._batch_power_states(prefs)
    # One list call per (sub, rg) tuple → 2 calls total, NOT 3.
    assert len(list_calls) == 2
    # Matched clusters carry the right power_state; unknown ones absent.
    assert states[("sub-1", "rg-elb", "cluster-a")] == "Running"
    assert states[("sub-1", "rg-elb", "cluster-b")] == "Stopped"
    # Summary attributes RG-level success/failure for log aggregation.
    assert summary["rg_groups"] == 2
    assert summary["rg_failed"] == 0
    assert summary["failed_rgs"] == []


def test_live_blast_signal_skips_probe_when_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped cluster has no API server — the live probe must be skipped
    entirely (the autouse stub is irrelevant here; we assert the early
    return BEFORE any probe call)."""
    from api.tasks.azure import idle_autostop

    # Remove the autouse stub so we exercise the real ``_live_blast_signal``
    # body and prove it never reaches the probe for a stopped cluster.
    monkeypatch.undo()
    probe_calls: list[Any] = []
    monkeypatch.setattr(
        idle_autostop,
        "probe_live_blast_activity",
        lambda pref: probe_calls.append(pref) or (5, None),
    )

    assert idle_autostop._live_blast_signal(_pref(), "Stopped") == (None, None)
    assert idle_autostop._live_blast_signal(_pref(), "") == (None, None)
    assert probe_calls == []
    # Running → probe is consulted.
    assert idle_autostop._live_blast_signal(_pref(), "Running") == (5, None)
    assert len(probe_calls) == 1


def test_auto_stop_aks_live_activity_blocks_idle_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OpenAPI-submitted BLAST run that the dashboard jobstate Table
    never saw still keeps an otherwise-idle cluster alive: the live K8s
    ``app=blast`` signal flows through the REAL evaluator and turns the
    stop into a keep with ``active_jobs:N``."""
    from datetime import UTC, datetime, timedelta

    from api.tasks.azure import idle_autostop

    now = datetime.now(UTC)
    save_auto_stop_preference(
        _pref(
            cluster_name="elb-cluster",
            enabled=True,
            cooldown_minutes=30,
            # Anchored long ago → idle window elapsed → would stop but for
            # the live signal.
            created_at=(now - timedelta(hours=4)).isoformat(timespec="seconds"),
        )
    )
    monkeypatch.setattr(idle_autostop, "_power_state", lambda _pref: "Running")
    monkeypatch.setattr(idle_autostop, "_provisioning_state", lambda _pref: "Succeeded")
    # Empty dashboard jobstate Table — the run is invisible to ``_scan_cluster_jobs``.
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: type("_R", (), {"list_for_scope": lambda self, **_kw: []})(),
    )
    # Live K8s probe reports two active ``app=blast`` jobs.
    monkeypatch.setattr(
        idle_autostop, "_live_blast_signal", lambda _pref, _ps: (2, None)
    )
    stop_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "api.tasks.azure.stop_aks.run",
        lambda **kwargs: stop_calls.append(kwargs) or {"status": "completed"},
    )

    result = idle_autostop.auto_stop_aks.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )
    assert result["action"] == "skip", result
    assert result["reason"] == "active_jobs:2"
    assert stop_calls == []
