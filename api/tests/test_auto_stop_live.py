"""Tests for `api.services.auto_stop_live`.

Responsibility: Cover `probe_live_blast_activity`'s in-use predicate and
    its fail-safe ``None`` return across the K8s status shapes
    `k8s_check_blast_status(job_id=None)` can produce.
Edit boundaries: Pure unit tests — `k8s_check_blast_status` and the
    credential seam are monkeypatched so no Azure/K8s call happens.
Key entry points: see per-test docstrings.
Risky contracts: A non-None return that over-reports activity would strand
    a cluster running forever; these tests pin the ``completed``/``failed``
    lingering-pod case to "not in use".
Validation: `uv run pytest -q api/tests/test_auto_stop_live.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import api.services.auto_stop_live as live_mod
from api.services.auto_stop import AutoStopPreference
from api.services.auto_stop_live import probe_live_blast_activity


def _pref() -> AutoStopPreference:
    return AutoStopPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        enabled=True,
        idle_minutes=60,
        cooldown_minutes=30,
    )


def _patch_status(monkeypatch, status: object) -> None:
    """Make `k8s_check_blast_status` return ``status`` and stub the credential."""

    def _fake_status(*_args, **_kwargs):
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(
        "api.services.k8s.blast_status.k8s_check_blast_status", _fake_status
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())


def test_running_active_jobs_reported(monkeypatch) -> None:
    _patch_status(
        monkeypatch,
        {
            "status": "running",
            "active": 3,
            "pods": 3,
            "jobs": 1,
            "started_at": "2026-05-29T11:55:00Z",
        },
    )
    result = probe_live_blast_activity(_pref())
    assert result is not None
    active, latest = result
    assert active == 3
    assert latest == datetime(2026, 5, 29, 11, 55, tzinfo=UTC)


def test_creating_phase_counts_as_in_use(monkeypatch) -> None:
    """A just-submitted run (Job present, no started pod yet → active 0)
    is still in use — covers the OpenAPI submit window before pods run."""
    _patch_status(
        monkeypatch,
        {"status": "creating", "active": 0, "pods": 1, "jobs": 1},
    )
    result = probe_live_blast_activity(_pref())
    assert result is not None
    active, latest = result
    assert active == 1
    assert latest is None


def test_completed_run_not_in_use_but_seeds_anchor(monkeypatch) -> None:
    """A finished run whose pods linger must NOT block a stop (active 0),
    but its completion time seeds the idle anchor."""
    _patch_status(
        monkeypatch,
        {
            "status": "completed",
            "active": 0,
            "pods": 2,
            "jobs": 1,
            "succeeded": 1,
            "started_at": "2026-05-29T11:00:00Z",
            "completed_at": "2026-05-29T11:40:00Z",
        },
    )
    result = probe_live_blast_activity(_pref())
    assert result is not None
    active, latest = result
    assert active == 0
    assert latest == datetime(2026, 5, 29, 11, 40, tzinfo=UTC)


def test_failed_run_not_in_use(monkeypatch) -> None:
    _patch_status(
        monkeypatch,
        {
            "status": "failed",
            "active": 0,
            "pods": 1,
            "jobs": 1,
            "failed": 1,
            "completed_at": "2026-05-29T11:40:00Z",
        },
    )
    result = probe_live_blast_activity(_pref())
    assert result is not None
    active, _latest = result
    assert active == 0


def test_no_jobs_or_pods_not_in_use(monkeypatch) -> None:
    """The cluster-wide ``creating`` sentinel with pods/jobs == 0 means no
    run at all — must report 0 active and no anchor."""
    _patch_status(
        monkeypatch,
        {"status": "creating", "pods": 0, "jobs": 0},
    )
    result = probe_live_blast_activity(_pref())
    assert result == (0, None)


def test_unknown_status_returns_none(monkeypatch) -> None:
    """``status == 'unknown'`` means the helper swallowed a K8s error —
    return None so the caller falls back instead of blocking forever."""
    _patch_status(
        monkeypatch,
        {"status": "unknown", "pods": 0, "detail": "boom"},
    )
    assert probe_live_blast_activity(_pref()) is None


def test_exception_returns_none(monkeypatch) -> None:
    """Any exception (kubeconfig fetch, token refresh) → None fail-safe."""
    _patch_status(monkeypatch, RuntimeError("k8s unreachable"))
    assert probe_live_blast_activity(_pref()) is None


def test_non_dict_status_returns_none(monkeypatch) -> None:
    _patch_status(monkeypatch, None)
    assert probe_live_blast_activity(_pref()) is None


def test_parse_k8s_ts_handles_naive_and_invalid() -> None:
    assert live_mod._parse_k8s_ts(None) is None
    assert live_mod._parse_k8s_ts("not-a-date") is None
    aware = live_mod._parse_k8s_ts("2026-05-29T11:55:00")
    assert aware is not None
    assert aware.tzinfo is not None
