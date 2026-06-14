"""Message Flow submit-time cache invalidation tests.

Responsibility: Verify a BLAST submit drops the read-side caches that gate how
    fast the new job surfaces on the dashboard Message Flow card, so a freshly
    submitted job does not wait out the monitor (~30s) / external-jobs (~70s)
    read caches.
Edit boundaries: Test-only. Exercises ``_invalidate_message_flow_caches`` and
    the ``/api/v1/elastic-blast/submit`` route wiring.
Key entry points: ``test_invalidate_message_flow_caches_*``.
Risky contracts: The monitor cache key prefix is ``monitor:message-flow``; the
    helper MUST invalidate every scope/limit variant under it and reset the
    external jobs cache. The helper MUST be best-effort (never raise).
Validation: ``uv run pytest -q api/tests/test_message_flow_cache_invalidation.py``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_invalidate_message_flow_caches_drops_monitor_prefix(monkeypatch) -> None:
    """The helper removes every ``monitor:message-flow:*`` snapshot and resets
    the external jobs cache."""
    from api.routes.blast.submit import _invalidate_message_flow_caches
    from api.services import monitor_cache

    monitor_cache.reset_monitor_snapshot_cache()
    # Seed two scope/limit variants the way the route caches them.
    monitor_cache.cached_snapshot("monitor:message-flow:shared:200", lambda: {"enabled": True})
    monitor_cache.cached_snapshot("monitor:message-flow:oid-1:50", lambda: {"enabled": True})
    # An unrelated monitor snapshot must be left intact.
    monitor_cache.cached_snapshot("monitor:aks:sub:rg", lambda: {"clusters": []})

    external_reset_called = {"n": 0}

    def _spy_reset() -> None:
        external_reset_called["n"] += 1

    monkeypatch.setattr(
        "api.services.blast.external_jobs._reset_external_jobs_cache", _spy_reset
    )

    _invalidate_message_flow_caches()

    # Both message-flow variants are gone (next read is a cold rebuild); the
    # unrelated AKS snapshot survives.
    removed = monitor_cache.invalidate_monitor_snapshot_prefix("monitor:message-flow")
    assert removed == 0, "message-flow entries should already have been removed"
    assert monitor_cache.invalidate_monitor_snapshot_prefix("monitor:aks") == 1
    assert external_reset_called["n"] == 1


def test_invalidate_message_flow_caches_is_best_effort(monkeypatch) -> None:
    """A failure in either underlying reset must not propagate."""
    from api.routes.blast import submit as submit_mod

    def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "api.services.blast.external_jobs._reset_external_jobs_cache", _boom
    )
    monkeypatch.setattr(
        "api.services.monitor_cache.invalidate_monitor_snapshot_prefix",
        lambda _prefix: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Must not raise.
    submit_mod._invalidate_message_flow_caches()


def test_external_submit_invalidates_message_flow_cache(monkeypatch) -> None:
    """The ``/api/v1/elastic-blast/submit`` route invalidates the message-flow
    snapshot so the new external job surfaces on the next card poll."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, monitor_cache

    monitor_cache.reset_monitor_snapshot_cache()
    monitor_cache.cached_snapshot("monitor:message-flow:shared:200", lambda: {"enabled": True})

    monkeypatch.setattr(external_blast, "ready", lambda **_kw: {"ready": True})
    monkeypatch.setattr(
        external_blast,
        "submit_job",
        lambda payload, **_kw: {"job_id": "ext-mf", "status": "queued"},
    )

    client = TestClient(app)
    resp = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGCATGC", "db": "core_nt"},
    )
    assert resp.status_code == 202

    # The seeded snapshot was dropped by the submit, so the next prefix
    # invalidation finds nothing left to remove.
    assert monitor_cache.invalidate_monitor_snapshot_prefix("monitor:message-flow") == 0
