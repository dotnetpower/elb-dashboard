"""Snapshot engine hardening tests.

Responsibility: Verify the diagnostics fetch layer isolates failures per
    resource and bounds a hung fetch by the per-fetch timeout instead of
    hanging the request.
Edit boundaries: Engine/snapshot behaviour only — rule specifics live in
    `test_diagnostics_rules.py`.
Key entry points: the `test_*` functions below.
Risky contracts: A hung fetch MUST return an `access="timeout"` snapshot within
    the per-fetch cap and never block past the run deadline.
Validation: `uv run pytest -q api/tests/test_diagnostics_snapshot.py`.
"""

from __future__ import annotations

import threading
import time

import pytest


def test_hung_fetch_times_out_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIAGNOSTICS_FETCH_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("DIAGNOSTICS_RUN_DEADLINE_SECONDS", "2")

    # Re-import so the module-level timeout constants pick up the env override.
    import importlib

    from api.services.diagnostics import snapshot as snap_mod

    importlib.reload(snap_mod)

    release = threading.Event()

    def _hang() -> dict:
        release.wait(timeout=5)
        return {"never": "returned in time"}

    def _ok() -> dict:
        return {"ok": True}

    snapshots: dict = {}
    started = time.monotonic()
    snap_mod._run_all({"aks": _hang, "storage": _ok}, snapshots)
    elapsed = time.monotonic() - started
    release.set()

    # The hung fetch is bounded by the per-fetch cap; the request does not block
    # for the full 5s the hang would otherwise take.
    assert elapsed < 2.0
    assert snapshots["aks"].access == "timeout"
    assert snapshots["aks"].available is False
    assert snapshots["storage"].available is True

    importlib.reload(snap_mod)  # restore defaults for other tests


def test_one_failure_does_not_block_siblings() -> None:
    from api.services.diagnostics import snapshot as snap_mod

    def _boom() -> dict:
        raise RuntimeError("nope")

    def _ok() -> dict:
        return {"ok": True}

    snapshots: dict = {}
    snap_mod._run_all({"acr": _boom, "storage": _ok}, snapshots)
    assert snapshots["acr"].available is False
    assert snapshots["acr"].access == "error"
    assert snapshots["storage"].available is True


def test_degraded_sidecar_metrics_become_unavailable_not_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis-unavailable all-down payload must NOT surface as healthy `down`
    sidecars — it is `unavailable` so the rule emits `indeterminate`, not a
    false `critical`."""
    from api.routes import monitor as monitor_package
    from api.services.diagnostics import snapshot as snap_mod

    monkeypatch.setattr(
        monitor_package,
        "collect_snapshot",
        lambda **k: {
            "degraded": True,
            "degraded_reason": "redis_unavailable",
            "sidecars": {"api": {"health": "down"}},
        },
    )
    result = snap_mod._sidecars_snapshot()
    assert result.available is False
    assert result.access == "error"
    assert "redis" in result.reason
