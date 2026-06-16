"""Tests for the message-flow snapshot service and route.

Responsibility: Verify ``build_message_flow`` returns a disabled shape when the
    integration is off, groups active jobs into producers by submitter alias,
    sizes broker boxes by query length, groups consumers by cluster, derives
    aliases for external/servicebus sources, and degrades Service Bus counts
    gracefully. Also covers the route's disabled default + auth gate.
Edit boundaries: Aggregation/route shaping only; persistence + SDK behaviour
    covered elsewhere.
Key entry points: the ``test_*`` functions.
Risky contracts: broker boxes reflect ACTIVE jobstate rows, never raw queue
    messages; aliases never expose a raw ``owner_oid``.
Validation: ``uv run pytest -q api/tests/test_message_flow.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from api.services import message_flow
from fastapi.testclient import TestClient


def _recent_iso(seconds_ago: float) -> str:
    """ISO timestamp ``seconds_ago`` before now (UTC), for settling-window tests."""
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _job(
    *,
    job_id: str,
    status: str,
    owner_upn: str | None = None,
    owner_oid: str | None = None,
    program: str = "blastn",
    db: str = "core_nt",
    cluster_name: str = "elb-cluster-01",
    resource_group: str = "rg-elb-cluster",
    subscription_id: str = "sub-1",
    payload: dict[str, Any] | None = None,
    phase: str | None = None,
    query_label: str | None = None,
    created_at: str = "2026-06-13T00:00:00+00:00",
    updated_at: str | None = None,
    error_code: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=job_id,
        status=status,
        owner_upn=owner_upn,
        owner_oid=owner_oid,
        program=program,
        db=db,
        cluster_name=cluster_name,
        resource_group=resource_group,
        subscription_id=subscription_id,
        phase=phase,
        query_label=query_label,
        created_at=created_at,
        updated_at=updated_at,
        error_code=error_code,
        payload=payload or {},
    )


class _FakeRepo:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def list_all(self, *, limit: int = 200, include_payload: bool = True) -> list[Any]:
        return self._rows[:limit]

    def list_for_owner(
        self, owner_oid: str, *, limit: int = 200, include_payload: bool = True
    ) -> list[Any]:
        return [r for r in self._rows if getattr(r, "owner_oid", None) == owner_oid][:limit]


@pytest.fixture()
def _enable(monkeypatch: pytest.MonkeyPatch):
    """Turn the integration on with a namespaced config and shared visibility."""

    def _apply(
        rows: list[Any], *, counts: Any = None, shared: bool = True, peek: Any = None
    ) -> None:
        monkeypatch.setattr(
            "api.services.service_bus_pref.service_bus_enabled", lambda: True
        )
        cfg = SimpleNamespace(
            namespace_fqdn="sb-elb-dashboard-krc.servicebus.windows.net",
            request_queue="elastic-blast-requests",
            completion_topic="elastic-blast-completions",
        )
        monkeypatch.setattr(
            "api.services.service_bus_pref.get_service_bus_config", lambda: cfg
        )
        monkeypatch.setattr(
            "api.services.blast.job_state.blast_shared_visibility_enabled", lambda: shared
        )
        monkeypatch.setattr(
            "api.services.state_repo.get_state_repo", lambda: _FakeRepo(rows)
        )

        from api.services import service_bus

        def _counts(_cfg: Any) -> dict[str, Any]:
            if counts is None:
                return {"queue": {"active_message_count": 0}, "subscriptions": []}
            if isinstance(counts, Exception):
                raise counts
            return counts

        monkeypatch.setattr(service_bus, "entity_counts", _counts)

        # Stub the non-destructive queue peek so the snapshot never reaches the
        # live AMQP data plane (slow + flaky). Defaults to an empty queue; a test
        # that exercises queue content passes ``peek=[...]``.
        def _peek(_cfg: Any, max_count: int = 10) -> list[dict[str, Any]]:
            if peek is None:
                return []
            if isinstance(peek, Exception):
                raise peek
            return list(peek)[:max_count]

        monkeypatch.setattr(service_bus, "peek_request_previews", _peek)

    return _apply


def test_disabled_when_integration_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.service_bus_pref.service_bus_enabled", lambda: False
    )
    assert message_flow.build_message_flow("oid-1") == {"enabled": False}


def test_active_jobs_grouped_into_producers_and_clusters(_enable) -> None:
    rows = [
        _job(
            job_id="j1",
            status="running",
            owner_upn="jihoon@example.com",
            payload={"submission_source": "dashboard", "query": {"total_letters": 12000}},
        ),
        _job(
            job_id="j2",
            status="queued",
            owner_upn="jihoon@example.com",
            payload={"submission_source": "dashboard", "query": {"total_letters": 400}},
        ),
        _job(
            job_id="j3",
            status="running",
            owner_upn="sora@example.com",
            payload={"submission_source": "dashboard", "query": {"query_count": 3}},
        ),
        # Completed job must be excluded from every lane.
        _job(job_id="j4", status="completed", owner_upn="jihoon@example.com"),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")

    assert snap["enabled"] is True
    assert snap["active_total"] == 3
    # Producers grouped by alias, busiest first.
    producers = snap["producers"]
    assert producers[0]["alias"] == "jihoon@example.com"
    assert producers[0]["job_count"] == 2
    assert {p["alias"] for p in producers} == {"jihoon@example.com", "sora@example.com"}
    # Broker boxes are the active rows only.
    assert {b["job_id"] for b in snap["broker"]} == {"j1", "j2", "j3"}
    sizes = {b["job_id"]: b["query_size"] for b in snap["broker"]}
    assert sizes["j1"] == 12000
    assert sizes["j2"] == 400
    assert sizes["j3"] == 3  # query_count fallback
    # Consumers grouped by cluster with running/queued split.
    clusters = snap["consumers"]["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["cluster_name"] == "elb-cluster-01"
    assert clusters[0]["running"] == 2
    assert clusters[0]["queued"] == 1
    assert clusters[0]["total"] == 3


def test_pending_and_reducing_are_active(_enable) -> None:
    """The broadened active set keeps ``pending`` and ``reducing`` jobs visible
    (a ``reducing`` job is still running its result-merge phase). ``reducing``
    folds into the consumer "running" badge, ``pending`` into "queued"."""
    rows = [
        _job(job_id="p1", status="pending", owner_upn="a@b.com"),
        _job(job_id="r1", status="reducing", owner_upn="a@b.com"),
        _job(job_id="run1", status="running", owner_upn="a@b.com"),
        _job(job_id="q1", status="queued", owner_upn="a@b.com"),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["active_total"] == 4
    assert {b["job_id"] for b in snap["broker"]} == {"p1", "r1", "run1", "q1"}
    assert all(b["lifecycle"] == "active" for b in snap["broker"])
    cluster = snap["consumers"]["clusters"][0]
    # running + reducing -> running badge; queued + pending -> queued badge.
    assert cluster["running"] == 2
    assert cluster["queued"] == 2
    assert cluster["total"] == 4
    assert cluster["settling"] == 0


def test_recently_terminal_jobs_settle_without_inflating_counts(_enable) -> None:
    """A just-finished/failed job lingers as a ``settling`` box with its real
    terminal status, but does NOT count toward producer/consumer active totals."""
    rows = [
        _job(
            job_id="run1",
            status="running",
            owner_upn="a@b.com",
            payload={"submission_source": "dashboard"},
        ),
        _job(
            job_id="done1",
            status="completed",
            owner_upn="a@b.com",
            updated_at=_recent_iso(10),
        ),
        _job(
            job_id="fail1",
            status="failed",
            owner_upn="a@b.com",
            updated_at=_recent_iso(20),
            error_code="database_not_found",
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["active_total"] == 1
    assert snap["settling_total"] == 2
    boxes = {b["job_id"]: b for b in snap["broker"]}
    assert boxes["run1"]["lifecycle"] == "active"
    assert boxes["done1"]["lifecycle"] == "settling"
    assert boxes["done1"]["status"] == "completed"
    assert boxes["fail1"]["lifecycle"] == "settling"
    assert boxes["fail1"]["status"] == "failed"
    assert boxes["fail1"]["error_code"] == "database_not_found"
    # Active boxes always come before settling ones.
    lifecycles = [b["lifecycle"] for b in snap["broker"]]
    assert lifecycles == ["active", "settling", "settling"]
    # Producers count active jobs only (1), not the settling pair.
    assert sum(p["job_count"] for p in snap["producers"]) == 1
    # Consumer running/queued reflect active only; settling tracked separately.
    cluster = snap["consumers"]["clusters"][0]
    assert cluster["running"] == 1
    assert cluster["queued"] == 0
    assert cluster["settling"] == 2
    assert cluster["total"] == 1


def test_old_terminal_jobs_excluded(_enable) -> None:
    """A terminal job older than the settling window is dropped entirely."""
    rows = [
        _job(
            job_id="old1",
            status="completed",
            owner_upn="a@b.com",
            updated_at=_recent_iso(600),  # 10 minutes ago, well past the 90s window
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["active_total"] == 0
    assert snap["settling_total"] == 0
    assert snap["broker"] == []


def test_settling_window_env_override(_enable, monkeypatch: pytest.MonkeyPatch) -> None:
    """``MESSAGE_FLOW_SETTLING_SECONDS`` tunes how long a terminal job lingers."""
    monkeypatch.setenv("MESSAGE_FLOW_SETTLING_SECONDS", "5")
    rows = [
        _job(
            job_id="done1",
            status="completed",
            owner_upn="a@b.com",
            updated_at=_recent_iso(20),  # outside the tightened 5s window
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["settling_total"] == 0


def test_consumers_dedup_same_cluster_when_rg_sub_backfilled(_enable) -> None:
    """One logical cluster split across rg-present/rg-absent rows merges into one
    card, and not-yet-placed jobs collapse into a single ``unassigned`` bucket."""
    rows = [
        # Placed, running row carries rg + sub.
        _job(
            job_id="r1",
            status="running",
            owner_upn="a@b.com",
            cluster_name="elb-cluster-01",
            resource_group="rg-elb-cluster",
            subscription_id="sub-1",
        ),
        # Same cluster, queued before rg/sub were backfilled (both empty).
        _job(
            job_id="r2",
            status="queued",
            owner_upn="a@b.com",
            cluster_name="elb-cluster-01",
            resource_group="",
            subscription_id="",
        ),
        # Two not-yet-placed jobs with no cluster name at all -> one bucket.
        _job(
            job_id="u1",
            status="queued",
            owner_upn="a@b.com",
            cluster_name="",
            resource_group="",
            subscription_id="",
        ),
        _job(
            job_id="u2",
            status="queued",
            owner_upn="a@b.com",
            cluster_name="",
            resource_group="rg-other",
            subscription_id="sub-9",
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    clusters = {c["cluster_name"]: c for c in snap["consumers"]["clusters"]}
    # elb-cluster-01 is a single card despite the rg-present / rg-absent split.
    assert "elb-cluster-01" in clusters
    named = clusters["elb-cluster-01"]
    assert named["running"] == 1
    assert named["queued"] == 1
    assert named["total"] == 2
    # rg/sub backfilled from the running row.
    assert named["resource_group"] == "rg-elb-cluster"
    assert named["subscription_id"] == "sub-1"
    # Both empty-name jobs collapse into a single "unassigned" bucket.
    assert "" in clusters
    unassigned = clusters[""]
    assert unassigned["queued"] == 2
    assert unassigned["total"] == 2
    # Exactly two consumer cards total (named + unassigned).
    assert len(snap["consumers"]["clusters"]) == 2


def test_external_and_servicebus_aliases(_enable) -> None:
    rows = [
        _job(
            job_id="sb1",
            status="running",
            owner_upn=None,
            payload={"submission_source": "servicebus"},
        ),
        _job(
            job_id="ext1",
            status="running",
            owner_upn=None,
            payload={"metadata": {"submission_source": "external_api"}},
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    aliases = {p["alias"] for p in snap["producers"]}
    assert aliases == {"servicebus", "external"}


def test_external_synced_row_labels_producer_external(_enable) -> None:
    """A `/v1/jobs` row synced into the Table stores the sibling job under
    ``payload={"external": job}`` with no top-level ``submission_source``. It
    must label its producer as ``external`` (external_api), not default to a
    dashboard user."""
    rows = [
        _job(
            job_id="ext-sync-1",
            status="running",
            owner_upn="api",
            payload={"external": {"job_id": "ext-sync-1", "status": "running"}},
        ),
        # Even when the nested external block carries an explicit source.
        _job(
            job_id="ext-sync-2",
            status="queued",
            owner_upn="api",
            payload={"external": {"submission_source": "external_api"}},
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    assert {p["alias"] for p in snap["producers"]} == {"external"}


def test_build_message_flow_syncs_external_jobs(monkeypatch, _enable) -> None:
    """``build_message_flow`` pulls external `/v1/jobs` rows into the Table via
    the shared orchestration before reading it, scoped to the platform
    subscription, so directly-submitted jobs surface without opening Recent
    searches."""
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "plat-sub")
    _enable([])

    calls: list[dict[str, Any]] = []

    def _fake_sync(**kwargs: Any):
        calls.append(kwargs)
        return message_flow_external_result()

    from api.services.blast import external_jobs

    monkeypatch.setattr(external_jobs, "collect_and_sync_external_jobs", _fake_sync)

    message_flow.build_message_flow("oid-x", tenant_id="tid-9")

    assert len(calls) == 1
    assert calls[0]["subscription_id"] == "plat-sub"
    assert calls[0]["tenant_id"] == "tid-9"
    assert calls[0]["detail_enrich_budget"] == 0


def test_build_message_flow_sync_is_best_effort(monkeypatch, _enable) -> None:
    """A discovery/sync failure must never break the snapshot — the card still
    renders the locally-known rows."""
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "plat-sub")
    rows = [
        _job(
            job_id="local-1",
            status="running",
            owner_upn="a@b.com",
            payload={"submission_source": "dashboard"},
        )
    ]
    _enable(rows)

    from api.services.blast import external_jobs

    def _boom(**_kwargs: Any):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(external_jobs, "collect_and_sync_external_jobs", _boom)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["enabled"] is True
    assert {b["job_id"] for b in snap["broker"]} == {"local-1"}


def test_build_message_flow_skips_sync_without_subscription(monkeypatch, _enable) -> None:
    """No ``AZURE_SUBSCRIPTION_ID`` → no discovery attempt (no-op)."""
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    _enable([])

    from api.services.blast import external_jobs

    calls: list[Any] = []
    monkeypatch.setattr(
        external_jobs,
        "collect_and_sync_external_jobs",
        lambda **kwargs: calls.append(kwargs),
    )

    message_flow.build_message_flow("oid-x")
    assert calls == []


def message_flow_external_result():
    """Minimal stand-in for ``ExternalJobsSyncResult`` (its fields are unused by
    the message-flow path, which re-reads the Table afterwards)."""
    return SimpleNamespace(
        rows=[], tombstoned_ids=set(), any_target_ok=True, target_failures=[],
        created=0, updated=0,
    )


    rows = [
        _job(
            job_id="j1",
            status="running",
            owner_upn=None,
            owner_oid="11111111-2222-3333-4444-555555555555",
            payload={"submission_source": "dashboard"},
        ),
    ]
    _enable(rows)

    snap = message_flow.build_message_flow("oid-x")
    alias = snap["producers"][0]["alias"]
    assert alias.startswith("user-")
    assert "11111111" not in alias


def test_query_size_none_when_absent(_enable) -> None:
    rows = [_job(job_id="j1", status="running", owner_upn="a@b.com", payload={})]
    _enable(rows)
    snap = message_flow.build_message_flow("oid-x")
    assert snap["broker"][0]["query_size"] is None


def test_counts_degrade_on_auth_error(_enable) -> None:
    from api.services import service_bus

    rows = [
        _job(
            job_id="j1",
            status="running",
            owner_upn="a@b.com",
            payload={"submission_source": "dashboard"},
        )
    ]
    _enable(rows, counts=service_bus.ServiceBusAuthError("no manage"))

    snap = message_flow.build_message_flow("oid-x")
    assert snap["sb_counts"]["available"] is False
    assert snap["sb_counts"]["reason"] == "no_manage_claim"


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.delenv("SERVICEBUS_ENABLED", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    from api.main import app

    return TestClient(app)


def test_route_disabled_default(client: TestClient) -> None:
    r = client.get("/api/monitor/message-flow")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_route_enabled_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The enabled route returns a full snapshot and never echoes a raw oid."""
    from api.services import service_bus
    from api.services.monitor_cache import reset_monitor_snapshot_cache

    reset_monitor_snapshot_cache()

    rows = [
        _job(
            job_id="j1",
            status="running",
            owner_upn="jihoon@example.com",
            owner_oid="11111111-2222-3333-4444-555555555555",
            payload={"submission_source": "dashboard", "query": {"total_letters": 9000}},
        ),
    ]
    monkeypatch.setattr(
        "api.services.service_bus_pref.service_bus_enabled", lambda: True
    )
    monkeypatch.setattr(
        "api.services.service_bus_pref.get_service_bus_config",
        lambda: SimpleNamespace(
            namespace_fqdn="sb-elb-dashboard-krc.servicebus.windows.net",
            request_queue="elastic-blast-requests",
            completion_topic="elastic-blast-completions",
        ),
    )
    monkeypatch.setattr(
        "api.services.blast.job_state.blast_shared_visibility_enabled", lambda: True
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _FakeRepo(rows)
    )
    monkeypatch.setattr(
        service_bus, "entity_counts", lambda _cfg: {"queue": {"active_message_count": 0}}
    )

    r = client.get("/api/monitor/message-flow")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["scope"] == "shared"
    assert body["active_total"] == 1
    assert body["broker_truncated"] is False
    assert len(body["broker"]) == 1
    # Snapshot must not leak the raw owner GUID anywhere.
    assert "11111111-2222-3333-4444-555555555555" not in r.text

    reset_monitor_snapshot_cache()


def test_snapshot_includes_dlq_delta_after_a_read(_enable, monkeypatch) -> None:
    """A successful counts read records a DLQ sample; the snapshot exposes the
    rolling-window delta. ``baseline_dlq == current_dlq`` on the first call
    because we never extrapolate past observed data."""
    from api.services import service_bus_telemetry

    service_bus_telemetry.reset_for_tests()
    rows = [
        _job(
            job_id="r1",
            status="running",
            owner_upn="a@b.com",
            payload={"submission_source": "dashboard"},
        ),
    ]
    _enable(
        rows,
        counts={
            "queue": {
                "active_message_count": 0,
                "dead_letter_message_count": 4,
                "scheduled_message_count": 0,
                "total_message_count": 4,
            },
            "subscriptions": [],
        },
    )

    snap = message_flow.build_message_flow("oid-x")
    delta = snap["dlq_delta"]
    assert delta is not None
    assert delta["samples"] == 1
    assert delta["baseline_dlq"] == 4
    assert delta["current_dlq"] == 4
    assert delta["delta"] == 0


def test_snapshot_dlq_delta_is_none_when_counts_unavailable(_enable) -> None:
    """If counts come back unavailable (no manage claim, transient error, …)
    no sample is recorded, so ``dlq_delta`` is ``None``."""
    from api.services import service_bus_telemetry

    service_bus_telemetry.reset_for_tests()
    rows = [
        _job(
            job_id="r1",
            status="running",
            owner_upn="a@b.com",
            payload={"submission_source": "dashboard"},
        ),
    ]
    from api.services import service_bus

    _enable(rows, counts=service_bus.ServiceBusAuthError("forbidden"))

    snap = message_flow.build_message_flow("oid-x")
    assert snap["dlq_delta"] is None
    assert snap["sb_counts"]["available"] is False


def test_snapshot_includes_queue_message_previews(_enable) -> None:
    """Peeked request-queue messages ride along on the snapshot as content."""
    rows: list[Any] = []
    preview = [
        {
            "message_id": "m1",
            "program": "blastn",
            "db": "core_nt",
            "body_preview": "{\"db\": \"core_nt\"}",
            "body_truncated": False,
        }
    ]
    _enable(rows, counts={"queue": {"active_message_count": 1}, "subscriptions": []}, peek=preview)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["queue_messages"] == preview


def test_queue_peek_skipped_when_namespace_unreachable(_enable) -> None:
    """When counts fail as 'unavailable' (namespace unreachable) the snapshot
    must NOT pay a second slow peek connect — queue_messages stays empty even if
    peek would have returned content."""
    from api.services import service_bus

    sentinel = [{"message_id": "should-not-appear"}]
    _enable(
        [],
        counts=service_bus.ServiceBusUnavailable("namespace unreachable"),
        peek=sentinel,
    )

    snap = message_flow.build_message_flow("oid-x")
    assert snap["queue_messages"] == []


def test_queue_peek_runs_when_only_manage_claim_missing(_enable) -> None:
    """A reachable namespace whose credential lacks the Manage claim still peeks
    (data-plane Receiver claim is enough), so content surfaces even when counts
    degrade to no_manage_claim."""
    from api.services import service_bus

    preview = [{"message_id": "m9", "db": "core_nt"}]
    _enable([], counts=service_bus.ServiceBusAuthError("no manage"), peek=preview)

    snap = message_flow.build_message_flow("oid-x")
    assert snap["sb_counts"]["available"] is False
    assert snap["sb_counts"]["reason"] == "no_manage_claim"
    assert snap["queue_messages"] == preview
