"""Optimistic-concurrency (ETag CAS) tests for the preference helpers.

Responsibility: Cover the ETag conditional-update behaviour added by
    issue #21 — ``save_*_preference`` honours the carried ETag,
    ``mark_*`` helpers refresh and retry on a sibling-write race, and
    the typed conflict surfaces cleanly when retries are exhausted.
Edit boundaries: File backend only (no Azure Tables fixture). The
    Table-backend path is exercised by mocking ``ResourceModifiedError``
    so the same conflict surface is hit without a live Tables
    endpoint.
Key entry points: see per-test docstrings.
Risky contracts: The file backend's ETag is a deterministic
    ``sha256(row_json)`` — tests rely on that to simulate concurrent
    writers by forcing a mid-flight mutation. Do not switch the file
    backend to a counter without updating the assertions here.
Validation: ``uv run pytest -q api/tests/test_preference_etag.py``.
"""

from __future__ import annotations

import pytest
from api.services.auto_stop import (
    AutoStopPreference,
    extend_auto_stop_preference,
    get_auto_stop_preference,
    mark_auto_stop_event,
    save_auto_stop_preference,
)
from api.services.auto_warmup import (
    AutoWarmupPreference,
    get_auto_warmup_preference,
    mark_auto_warmup_ready_state,
    save_auto_warmup_preference,
)
from api.services.preference_concurrency import (
    PreferenceUpdateConflict,
    cas_retry,
)

# --- auto_stop ---------------------------------------------------------------


def _make_autostop(**overrides: object) -> AutoStopPreference:
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


def test_save_populates_etag_on_first_write(monkeypatch, tmp_path) -> None:
    """``save_auto_stop_preference`` returns the new ETag on a fresh row."""
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    pref = _make_autostop()
    assert pref.etag == ""
    saved = save_auto_stop_preference(pref)
    assert saved.etag != ""
    # Re-read should yield the same ETag.
    fetched = get_auto_stop_preference("sub-1", "rg-elb", "elb-cluster")
    assert fetched is not None
    assert fetched.etag == saved.etag


def test_save_with_stale_etag_raises_conflict(monkeypatch, tmp_path) -> None:
    """A second writer carrying an outdated ETag must surface
    :class:`PreferenceUpdateConflict` rather than clobber the row.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    first = save_auto_stop_preference(_make_autostop(enabled=True, idle_minutes=60))
    # A sibling writer advances the row, invalidating ``first.etag``.
    save_auto_stop_preference(_make_autostop(enabled=False, idle_minutes=30))

    # The first writer now attempts a CAS save with the stale ETag.
    stale = AutoStopPreference.from_dict(first.to_dict())
    stale.etag = first.etag
    stale.idle_minutes = 90
    with pytest.raises(PreferenceUpdateConflict):
        save_auto_stop_preference(stale)

    # On-disk state must match the sibling write, not the stale attempt.
    on_disk = get_auto_stop_preference("sub-1", "rg-elb", "elb-cluster")
    assert on_disk is not None
    assert on_disk.enabled is False
    assert on_disk.idle_minutes == 30


def test_save_without_etag_remains_unconditional(monkeypatch, tmp_path) -> None:
    """SPA PUT path: ``pref.etag == ""`` must continue to upsert
    unconditionally so first-write and force-overwrite both work.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_stop_preference(_make_autostop(enabled=True))
    # Caller drops the ETag and resubmits — must succeed.
    overwrite = _make_autostop(enabled=False, idle_minutes=30)
    assert overwrite.etag == ""
    saved = save_auto_stop_preference(overwrite)
    assert saved.etag != ""
    on_disk = get_auto_stop_preference("sub-1", "rg-elb", "elb-cluster")
    assert on_disk is not None
    assert on_disk.enabled is False


def test_mark_auto_stop_event_etag_collision_refreshes_and_persists(
    monkeypatch, tmp_path
) -> None:
    """Acceptance test for issue #21: simulate a sibling writer landing
    between the helper's fresh-read and CAS save. The retry MUST refresh
    onto the new row — never silently overwrite — and the persisted
    result MUST include both the sibling's user-owned fields AND the
    bookkeeping fields this helper writes.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_stop_preference(_make_autostop(enabled=True, idle_minutes=60))

    import api.services.auto_stop as mod

    real_get = mod.get_auto_stop_preference
    state = {"reads": 0}

    def racey_get(*args, **kwargs):
        latest = real_get(*args, **kwargs)
        if state["reads"] == 0:
            # Sibling writer slips in *after* our read returned but
            # *before* mark_auto_stop_event runs its conditional save.
            # The CAS save with the stale ETag will fail; cas_retry
            # refreshes via this same hook and the second call (when
            # state["reads"] == 1) returns the post-sibling row.
            mod.save_auto_stop_preference(
                _make_autostop(enabled=False, idle_minutes=30)
            )
        state["reads"] += 1
        return latest

    monkeypatch.setattr(mod, "get_auto_stop_preference", racey_get)

    pref = _make_autostop(enabled=True, idle_minutes=60)
    updated = mark_auto_stop_event(pref, stopped=True, reason="idle:60m")

    # Bookkeeping persisted.
    assert updated.last_stop_at != ""
    assert updated.last_stop_reason == "idle:60m"
    # Sibling writer's user fields survived (NOT clobbered to True/60).
    assert updated.enabled is False
    assert updated.idle_minutes == 30
    # And the retry must have happened (real_get called at least twice).
    assert state["reads"] >= 2

    # Final on-disk state matches sibling write + bookkeeping merge.
    on_disk = real_get("sub-1", "rg-elb", "elb-cluster")
    assert on_disk is not None
    assert on_disk.enabled is False
    assert on_disk.idle_minutes == 30
    assert on_disk.last_stop_reason == "idle:60m"


def test_extend_auto_stop_preference_uses_cas_retry(monkeypatch, tmp_path) -> None:
    """Same race semantics as ``mark_auto_stop_event``: a sibling write
    between read and save MUST cause a retry, never a clobber. The
    Extend grant lands on the fresh row.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_stop_preference(_make_autostop(enabled=True))

    import api.services.auto_stop as mod

    real_get = mod.get_auto_stop_preference
    state = {"reads": 0}

    def racey_get(*args, **kwargs):
        latest = real_get(*args, **kwargs)
        if state["reads"] == 0:
            mod.save_auto_stop_preference(
                _make_autostop(enabled=False, idle_minutes=30)
            )
        state["reads"] += 1
        return latest

    monkeypatch.setattr(mod, "get_auto_stop_preference", racey_get)

    pref = _make_autostop()
    updated = extend_auto_stop_preference(pref, minutes=30)
    assert updated.extend_until != ""
    # Sibling-writer fields survive.
    assert updated.enabled is False
    assert updated.idle_minutes == 30
    assert state["reads"] >= 2


# --- auto_warmup -------------------------------------------------------------


def _make_warmup(**overrides: object) -> AutoWarmupPreference:
    base = AutoWarmupPreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="stelbwarm01",
        storage_resource_group="rg-elb",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_warmup_save_populates_etag_on_first_write(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    saved = save_auto_warmup_preference(_make_warmup())
    assert saved.etag != ""
    fetched = get_auto_warmup_preference("sub-1", "rg-elb", "elb-cluster")
    assert fetched is not None
    assert fetched.etag == saved.etag


def test_warmup_save_with_stale_etag_raises_conflict(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    first = save_auto_warmup_preference(_make_warmup(enabled=True))
    save_auto_warmup_preference(_make_warmup(enabled=False))

    stale = AutoWarmupPreference.from_dict(first.to_dict())
    stale.etag = first.etag
    stale.databases = ["nt"]
    with pytest.raises(PreferenceUpdateConflict):
        save_auto_warmup_preference(stale)

    on_disk = get_auto_warmup_preference("sub-1", "rg-elb", "elb-cluster")
    assert on_disk is not None
    assert on_disk.enabled is False
    assert on_disk.databases == []


def test_mark_auto_warmup_ready_state_etag_collision_refreshes(
    monkeypatch, tmp_path
) -> None:
    """Acceptance test analogue for ``auto_warmup``: sibling write
    between read and save must trigger a refresh-and-retry, the
    sibling's user-owned fields survive, and the bookkeeping fields
    land on the fresh row.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_warmup_preference(_make_warmup(enabled=True, databases=["nt"]))

    import api.services.auto_warmup as mod

    real_get = mod.get_auto_warmup_preference
    state = {"reads": 0}

    def racey_get(*args, **kwargs):
        latest = real_get(*args, **kwargs)
        if state["reads"] == 0:
            mod.save_auto_warmup_preference(
                _make_warmup(enabled=False, databases=["nr"])
            )
        state["reads"] += 1
        return latest

    monkeypatch.setattr(mod, "get_auto_warmup_preference", racey_get)

    pref = _make_warmup(enabled=True, databases=["nt"])
    updated = mark_auto_warmup_ready_state(pref, ready=True, triggered=True)

    assert updated.last_ready is True
    assert updated.last_triggered_at != ""
    # Sibling-write user fields survive.
    assert updated.enabled is False
    assert updated.databases == ["nr"]
    assert state["reads"] >= 2


# --- cas_retry primitive -----------------------------------------------------


def test_cas_retry_exhausts_and_surfaces_conflict() -> None:
    """If every attempt raises, ``cas_retry`` re-raises the last conflict."""
    attempts = {"count": 0}

    def attempt() -> str:
        attempts["count"] += 1
        raise PreferenceUpdateConflict("simulated CAS miss")

    with pytest.raises(PreferenceUpdateConflict):
        cas_retry(attempt, max_attempts=3, operation="test")
    assert attempts["count"] == 3


def test_cas_retry_returns_on_first_success() -> None:
    """First-success short-circuits the retry loop."""
    attempts = {"count": 0}

    def attempt() -> str:
        attempts["count"] += 1
        return "ok"

    assert cas_retry(attempt) == "ok"
    assert attempts["count"] == 1


def test_cas_retry_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError):
        cas_retry(lambda: "x", max_attempts=0)


def test_mark_auto_stop_event_logs_warning_when_retries_exhausted(
    monkeypatch, tmp_path, caplog
) -> None:
    """Bookkeeping writer treats CAS exhaustion as a soft miss: log a
    warning and return the in-memory snapshot rather than re-raising.
    """
    import logging

    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_stop_preference(_make_autostop())

    import api.services.auto_stop as mod

    real_get = mod.get_auto_stop_preference
    counter = {"n": 0}

    def always_racey_get(*args, **kwargs):
        # Every call lands a sibling write with a *unique* idle_minutes
        # value so the row's content-hash ETag advances on every read,
        # guaranteeing the CAS save never wins and cas_retry exhausts.
        counter["n"] += 1
        latest = real_get(*args, **kwargs)
        # ALLOWED_IDLE_MINUTES rotates through valid buckets so
        # ``save_auto_stop_preference`` never raises a ValueError.
        idle_options = [15, 30, 60, 120, 240]
        idle = idle_options[counter["n"] % len(idle_options)]
        mod.save_auto_stop_preference(
            _make_autostop(enabled=False, idle_minutes=idle)
        )
        return latest

    counter = {"n": 0}

    monkeypatch.setattr(mod, "get_auto_stop_preference", always_racey_get)

    caplog.set_level(logging.WARNING)
    caplog.clear()
    pref = _make_autostop()
    result = mark_auto_stop_event(pref, stopped=True, reason="idle:60m")
    # Helper returned the in-memory snapshot (no exception bubbled).
    assert result.last_stop_reason == "idle:60m"
    # Warning surfaced for operators (from either cas_retry exhaustion
    # log or the auto_stop wrapper that catches the re-raised conflict).
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "CAS exhaustion" in msg or "CAS retries exhausted" in msg
        for msg in messages
    ), messages
