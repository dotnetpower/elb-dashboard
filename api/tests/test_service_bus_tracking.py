"""Tests for the Service Bus bridge atomic claim / release (file backend).

Responsibility: Verify the single-writer reservation contract that makes a
    parallel / multi-worker drain safe — first claim wins, a fresh reservation
    blocks a second claim, a confirmed row is never re-claimable, release rolls
    an unconfirmed reservation back, release never deletes a confirmed row, and
    a stale unconfirmed reservation can be stolen so a crashed worker cannot
    wedge a correlation id forever.
Edit boundaries: Exercises the JSON file backend (no live Azure Table); forces
    it by unsetting AZURE_TABLE_ENDPOINT and pointing ELB_LOCAL_STATE_DIR at a
    tmp dir.
Key entry points: the ``test_*`` functions.
Risky contracts: at most one caller ever wins a given correlation id while a
    fresh reservation is held; confirmed rows are immutable to claim/release.
Validation: ``uv run pytest -q api/tests/test_service_bus_tracking.py``.
"""

from __future__ import annotations

import pytest
from api.services import service_bus_tracking as t
from api.services.service_bus_tracking import BridgeRecord


@pytest.fixture(autouse=True)
def _local_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # Force the file backend: _use_table_backend() requires BOTH env vars.
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    yield


def test_first_claim_wins_and_fresh_reservation_blocks_second() -> None:
    assert t.claim_bridge("corr-a", "req-1") is True
    # A fresh, unconfirmed reservation is held → a concurrent claim must lose so
    # only the winner submits.
    assert t.claim_bridge("corr-a") is False


def test_confirmed_row_is_never_reclaimable() -> None:
    assert t.claim_bridge("corr-b") is True
    t.upsert_bridge(BridgeRecord(correlation_id="corr-b", openapi_job_id="job-1"))
    # Confirmed (has an openapi_job_id) → claim must refuse; re-claiming would be
    # a duplicate BLAST submit.
    assert t.claim_bridge("corr-b") is False


def test_release_unconfirmed_allows_reclaim() -> None:
    assert t.claim_bridge("corr-c") is True
    t.release_bridge("corr-c")
    # Reservation rolled back → a redelivery can re-claim and resubmit.
    assert t.claim_bridge("corr-c") is True


def test_release_never_deletes_a_confirmed_row() -> None:
    assert t.claim_bridge("corr-d") is True
    t.upsert_bridge(BridgeRecord(correlation_id="corr-d", openapi_job_id="job-d"))
    t.release_bridge("corr-d")  # must be a no-op on a confirmed row
    rec = t.get_bridge("corr-d")
    assert rec is not None
    assert rec.openapi_job_id == "job-d"


def test_stale_unconfirmed_reservation_is_stealable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # -1s threshold makes any existing reservation immediately stale.
    monkeypatch.setattr(t, "_CLAIM_STALE_SECONDS", -1)
    assert t.claim_bridge("corr-e") is True
    # The prior reservation is now stale → a second worker may steal it (so a
    # worker that crashed mid-submit cannot reserve the id forever).
    assert t.claim_bridge("corr-e") is True


def test_fresh_reservation_is_not_stolen_under_default_threshold() -> None:
    assert t.claim_bridge("corr-f") is True
    # Default threshold (>=30s) → a just-made reservation is NOT stale, so a
    # racing claim still loses (no accidental double submit).
    assert t.claim_bridge("corr-f") is False


def test_claim_stale_seconds_env_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVICEBUS_CLAIM_STALE_SECONDS", "not-a-number")
    # A bad override must not crash import; it falls back to the 180s default.
    assert t._claim_stale_seconds_from_env() == 180


def test_claim_stale_seconds_env_is_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICEBUS_CLAIM_STALE_SECONDS", "5")
    # Floored at 30s so a too-small override can never steal a still-submitting
    # reservation out from under a live worker.
    assert t._claim_stale_seconds_from_env() == 30
