"""Tests for the upgrade-state persistence helpers (in-memory backend).

Module summary: Drives the read/update path of `api.services.upgrade.state`
against the in-memory backend so no Azure Tables endpoint is required.
Validates ETag CAS behaviour and the public-dict serialiser.

Responsibility: Verify state-row CRUD invariants without touching Azure.
Edit boundaries: Update these tests when the state schema or CAS contract
  changes.
Key entry points: Test functions for defaults, round-trip, mutate, CAS.
Risky contracts: Asserts that `RowEtagMismatch` is raised on stale writes
  so the upgrade flow can rely on optimistic concurrency.
Validation: `uv run pytest -q api/tests/test_upgrade_state.py`.
"""

from __future__ import annotations

import json

import pytest
from api.services.upgrade import state


@pytest.fixture(autouse=True)
def _in_memory_backend() -> None:
    state.set_backend(state.InMemoryBackend())
    yield
    state.set_backend(None)


def test_get_state_returns_defaults_when_empty() -> None:
    s = state.get_state()
    assert s.state == state.STATE_IDLE
    assert s.running_version == ""
    assert s.latest_version == ""
    assert s.current_images() == {}
    assert s.rollback_target() == {}
    assert s.etag == ""


def test_update_state_persists_mutation() -> None:
    def mutate(s: state.UpgradeState) -> None:
        s.latest_version = "0.3.0"
        s.latest_sha = "f" * 40
        s.git_remote = "https://example.test/foo.git"

    after = state.update_state(mutate)
    assert after.latest_version == "0.3.0"
    assert after.git_remote == "https://example.test/foo.git"
    # ETag is set after first write.
    assert after.etag

    again = state.get_state()
    assert again.latest_version == "0.3.0"
    assert again.etag == after.etag


def test_update_state_writes_updated_at() -> None:
    after = state.update_state(lambda s: setattr(s, "phase_detail", "hello"))
    assert after.updated_at
    assert after.phase_detail == "hello"


def test_to_public_dict_expands_json_fields() -> None:
    def mutate(s: state.UpgradeState) -> None:
        s.current_images_json = json.dumps({"api": "myacr.azurecr.io/elb-api:v0.2.0"})
        s.rollback_target_json = json.dumps({"frontend": "myacr.azurecr.io/elb-frontend:v0.1.9"})

    after = state.update_state(mutate)
    pub = after.to_public_dict()
    assert pub["current_images"] == {"api": "myacr.azurecr.io/elb-api:v0.2.0"}
    assert pub["rollback_target"] == {"frontend": "myacr.azurecr.io/elb-frontend:v0.1.9"}
    assert "current_images_json" not in pub
    assert "rollback_target_json" not in pub
    assert "etag" not in pub


def test_cas_detects_concurrent_writer() -> None:
    state.update_state(lambda s: setattr(s, "phase_detail", "initial"))

    stale = state.get_state()  # captures the current ETag
    # A concurrent writer races us and bumps the row.
    state.update_state(lambda s: setattr(s, "phase_detail", "from-other"))

    # Now we try to write using the stale ETag — simulate by going through
    # the backend directly with the captured etag.
    stale.phase_detail = "from-me"
    with pytest.raises(state.RowEtagMismatch):
        state._backend().upsert(stale, expected_etag=stale.etag)


def test_first_write_race_is_refused() -> None:
    """Two concurrent first-ever writes: only the first must succeed.

    Before the fix the backend issued an unconditional `upsert_entity` on
    no-etag writes, so two operators racing past `cas_state(IDLE ->
    QUEUED)` on a fresh deployment would silently overwrite each other on
    the single shared row. The fix maps no-etag-with-existing-row to a
    `RowEtagMismatch` so `cas_state`'s retry observes the row that the
    first writer created and refuses the second start.
    """
    fresh = state.UpgradeState(state=state.STATE_QUEUED, job_id="first")
    state._backend().upsert(fresh, expected_etag="")
    second = state.UpgradeState(state=state.STATE_QUEUED, job_id="second")
    with pytest.raises(state.RowEtagMismatch):
        state._backend().upsert(second, expected_etag="")


def test_load_json_dict_tolerates_bad_payloads() -> None:
    assert state._load_json_dict("") == {}
    assert state._load_json_dict("not json") == {}
    assert state._load_json_dict("[1,2]") == {}
    assert state._load_json_dict('{"a":1}') == {"a": "1"}
