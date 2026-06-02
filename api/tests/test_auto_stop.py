"""Tests for `api.services.auto_stop`.

Responsibility: Exercise the preference storage round-trip (file backend),
    `normalise_preference` validation, `is_extended`/`is_in_cooldown`
    helpers, and `mark_auto_stop_event` / `extend_auto_stop_preference`
    mutations.
Edit boundaries: No HTTP, no Azure Tables — file backend only (the table
    backend has the same code path as `auto_warmup` and is exercised
    indirectly there). Add Table coverage when behaviour diverges.
Key entry points: see per-test docstrings.
Risky contracts: ``preference_key`` shape is shared with the Table
    PartitionKey — locked here so a future rename breaks loudly.
Validation: `uv run pytest -q api/tests/test_auto_stop.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from api.services.auto_stop import (
    ALLOWED_IDLE_MINUTES,
    DEFAULT_IDLE_MINUTES,
    AutoStopPreference,
    extend_auto_stop_preference,
    get_auto_stop_preference,
    is_extended,
    is_in_cooldown,
    list_auto_stop_preferences,
    mark_auto_stop_event,
    mark_auto_stop_started,
    normalise_preference,
    preference_key,
    save_auto_stop_preference,
)


def _make(**overrides: object) -> AutoStopPreference:
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


def test_preference_key_is_stable_and_safe() -> None:
    key = preference_key("sub/with weird chars!", "rg-elb", "elb-cluster")
    assert key.startswith("auto_stop:")
    # No raw slashes / spaces / punctuation that would break Azure Tables PartitionKey.
    assert "/" not in key
    assert " " not in key
    assert "!" not in key


def test_normalise_preference_clamps_idle_minutes() -> None:
    pref = normalise_preference(
        {
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 17,  # not in ALLOWED_IDLE_MINUTES
        }
    )
    assert pref.idle_minutes in ALLOWED_IDLE_MINUTES
    # Should pick the nearest allowed bucket (15 in this case).
    assert pref.idle_minutes == 15


def test_normalise_preference_rejects_missing_scope() -> None:
    with pytest.raises(ValueError):
        normalise_preference(
            {"subscription_id": "", "resource_group": "rg", "cluster_name": "c"}
        )


def test_save_and_get_roundtrip_file_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    pref = _make(idle_minutes=30, enabled=True, owner_oid="oid-1")
    saved = save_auto_stop_preference(pref)
    assert saved.idle_minutes == 30

    loaded = get_auto_stop_preference("sub-1", "rg-elb", "elb-cluster")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.idle_minutes == 30
    assert loaded.owner_oid == "oid-1"


def test_file_backend_save_does_not_create_lock_sentinel(monkeypatch, tmp_path) -> None:
    """Critique #14: the file backend used to leave an orphan
    ``auto_stop.json.lock`` sentinel after every save. The fix swaps the
    sibling-file ``fcntl.flock`` pattern for an in-process
    ``threading.Lock`` keyed by the state file path, so no ``.lock``
    file is created at all.
    """
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_stop_preference(_make(enabled=True))
    save_auto_stop_preference(_make(cluster_name="elb-cluster-2", enabled=False))

    files = {p.name for p in tmp_path.iterdir()}
    assert "auto_stop.json" in files
    assert not any(name.endswith(".lock") for name in files), files


def test_list_auto_stop_preferences_file_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    save_auto_stop_preference(_make(cluster_name="cluster-a", enabled=True))
    save_auto_stop_preference(_make(cluster_name="cluster-b", enabled=False))

    rows = list_auto_stop_preferences(limit=10)
    names = sorted(row.cluster_name for row in rows)
    assert names == ["cluster-a", "cluster-b"]


def test_mark_auto_stop_event_records_stop_and_skip(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    pref = save_auto_stop_preference(_make())

    after_stop = mark_auto_stop_event(pref, stopped=True, reason="idle:60m")
    assert after_stop.last_stop_at != ""
    assert after_stop.last_stop_reason == "idle:60m"
    assert after_stop.last_skip_at == ""

    after_skip = mark_auto_stop_event(after_stop, stopped=False, reason="active_jobs:2")
    assert after_skip.last_skip_at != ""
    assert after_skip.last_skip_reason == "active_jobs:2"
    # Stop fields are preserved across a subsequent skip.
    assert after_skip.last_stop_at == after_stop.last_stop_at


def test_mark_auto_stop_started_stamps_last_started_at(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    pref = save_auto_stop_preference(_make())
    assert pref.last_started_at == ""

    updated = mark_auto_stop_started(
        pref.subscription_id, pref.resource_group, pref.cluster_name
    )
    assert updated is not None
    assert updated.last_started_at != ""
    # Persisted, not just returned.
    reloaded = get_auto_stop_preference(
        pref.subscription_id, pref.resource_group, pref.cluster_name
    )
    assert reloaded is not None
    assert reloaded.last_started_at == updated.last_started_at


def test_mark_auto_stop_started_noop_when_no_pref(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    result = mark_auto_stop_started("sub-x", "rg-x", "cluster-x")
    assert result is None


def test_last_started_at_round_trips_through_dict() -> None:
    pref = _make(last_started_at="2026-06-02T07:22:14+00:00")
    restored = AutoStopPreference.from_dict(pref.to_dict())
    assert restored.last_started_at == "2026-06-02T07:22:14+00:00"
    assert restored == pref


def test_extend_auto_stop_preference_sets_future_deadline(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    pref = save_auto_stop_preference(_make())

    extended = extend_auto_stop_preference(pref, minutes=30)
    assert extended.extend_until != ""
    deadline = datetime.fromisoformat(extended.extend_until)
    assert deadline.tzinfo is not None
    # Allow a small tolerance for execution time.
    assert deadline > datetime.now(UTC) + timedelta(minutes=25)


def test_is_extended_true_until_grant_expires() -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    pref = _make(extend_until=(now + timedelta(minutes=10)).isoformat(timespec="seconds"))
    assert is_extended(pref, now=now)
    assert not is_extended(pref, now=now + timedelta(minutes=20))


def test_is_in_cooldown_blocks_immediate_restop() -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    pref = _make(
        last_stop_at=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        cooldown_minutes=30,
    )
    assert is_in_cooldown(pref, now=now)
    later = now + timedelta(minutes=35)
    assert not is_in_cooldown(pref, now=later)


def test_default_idle_minutes_is_60() -> None:
    """Charter default — see docs/features_change/2026-05/2026-05-29-aks-idle-auto-stop.md."""
    assert DEFAULT_IDLE_MINUTES == 60
    assert DEFAULT_IDLE_MINUTES in ALLOWED_IDLE_MINUTES


def test_mark_auto_stop_event_does_not_clobber_user_toggle(
    monkeypatch, tmp_path
) -> None:
    """Lost-update guard: if the user toggled ``enabled=False`` between
    the beat task's pref-read and its pref-write, the bookkeeping write
    MUST NOT resurrect ``enabled=True`` from the in-memory snapshot."""
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    # Beat reads pref (enabled=True, idle_minutes=60).
    beat_view = save_auto_stop_preference(_make(enabled=True, idle_minutes=60))
    # ... long pause ...
    # User toggles the row off via PUT (race window).
    save_auto_stop_preference(_make(enabled=False, idle_minutes=30))
    # Beat now writes its stop-event using the stale `beat_view`.
    updated = mark_auto_stop_event(beat_view, stopped=True, reason="idle:60m")
    # Bookkeeping field IS written.
    assert updated.last_stop_at != ""
    assert updated.last_stop_reason == "idle:60m"
    # But user-owned fields were NOT clobbered.
    assert updated.enabled is False
    assert updated.idle_minutes == 30


def test_mark_auto_stop_event_noop_when_row_deleted(monkeypatch, tmp_path) -> None:
    """If the user deleted the pref mid-tick, the bookkeeping write must
    not silently re-create the row."""
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))

    # Beat reads pref. (We do NOT save it — simulate "row already vanished".)
    beat_view = _make()
    # Bookkeeping write must not resurrect.
    mark_auto_stop_event(beat_view, stopped=True, reason="idle:60m")
    assert get_auto_stop_preference("sub-1", "rg-elb", "elb-cluster") is None
