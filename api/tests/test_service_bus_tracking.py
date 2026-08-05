"""Tests for the Service Bus bridge atomic claim / release (file + Table backends).

Responsibility: Verify the single-writer reservation contract that makes a
    parallel / multi-worker drain safe — first claim wins, a fresh reservation
    blocks a second claim, a confirmed row is never re-claimable, release rolls
    an unconfirmed reservation back, release never deletes a confirmed row, and
    a stale unconfirmed reservation can be stolen so a crashed worker cannot
    wedge a correlation id forever. Also pins the Table-backend etag contract
    for the conditional steal / release writes.
Edit boundaries: Exercises the JSON file backend (no live Azure Table); forces
    it by unsetting AZURE_TABLE_ENDPOINT and pointing ELB_LOCAL_STATE_DIR at a
    tmp dir. The Table-backend cases use a fake TableClient plus a real
    ``TableEntity`` so the ``metadata["etag"]`` semantics stay faithful.
Key entry points: the ``test_*`` functions.
Risky contracts: at most one caller ever wins a given correlation id while a
    fresh reservation is held; confirmed rows are immutable to claim/release;
    conditional Table writes must carry a non-empty etag.
Validation: ``uv run pytest -q api/tests/test_service_bus_tracking.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from api.services import service_bus_tracking as t
from api.services.service_bus_tracking import BridgeRecord
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableEntity
from billiard.exceptions import SoftTimeLimitExceeded


@pytest.fixture(autouse=True)
def _local_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # Force the file backend: _use_table_backend() requires BOTH env vars.
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    yield


def test_first_claim_wins_and_fresh_reservation_blocks_second() -> None:
    assert t.claim_bridge("corr-a", "req-1", "fingerprint-a") is True
    record = t.get_bridge("corr-a")
    assert record is not None
    assert record.request_fingerprint == "fingerprint-a"
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
    # Floored above the sibling submit timeout so a too-small override can never
    # steal a still-submitting reservation out from under a live worker (which
    # would produce a duplicate BLAST run).
    monkeypatch.setenv("SERVICEBUS_CLAIM_STALE_SECONDS", "5")
    assert t._claim_stale_seconds_from_env() == t._CLAIM_STALE_FLOOR_SECONDS


def test_bridge_fingerprint_round_trips_without_request_payload() -> None:
    t.upsert_bridge(
        BridgeRecord(
            correlation_id="corr-fingerprint",
            openapi_job_id="job-fingerprint",
            request_fingerprint="a" * 64,
        )
    )
    record = t.get_bridge("corr-fingerprint")
    assert record is not None
    assert record.request_fingerprint == "a" * 64
    assert "query_fasta" not in record.to_dict()


def test_table_unsafe_correlation_ids_use_collision_safe_keys() -> None:
    spaced = t._row_key("wf3:943:exclusive:hypothetical protein:1024979")
    underscored = t._row_key("wf3:943:exclusive:hypothetical_protein:1024979")

    assert spaced.startswith("unsafe-")
    assert underscored == "wf3:943:exclusive:hypothetical_protein:1024979"
    assert spaced != underscored


def test_active_bridge_pages_rotate_without_starving_rows() -> None:
    for index in range(5):
        t.upsert_bridge(
            BridgeRecord(
                correlation_id=f"corr-{index}",
                openapi_job_id=f"job-{index}",
            )
        )

    first, cursor = t.list_active_bridges_page(limit=2)
    second, cursor = t.list_active_bridges_page(limit=2, after_row_key=cursor)
    third, _cursor = t.list_active_bridges_page(limit=2, after_row_key=cursor)

    seen = [record.correlation_id for record in first + second + third]
    assert seen[:5] == [f"corr-{index}" for index in range(5)]
    assert seen[5] == "corr-0"


def test_table_page_does_not_issue_zero_size_wrap_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, int]] = []

    class _Table:
        def query_entities(self, query_filter: str, *, results_per_page: int):
            queries.append((query_filter, results_per_page))
            return [
                t._entity(
                    BridgeRecord(
                        correlation_id=f"corr-{index}",
                        openapi_job_id=f"job-{index}",
                    )
                )
                for index in range(results_per_page)
            ]

    @contextmanager
    def _client():
        yield _Table()

    monkeypatch.setattr(t, "_ensure_table", lambda: None)
    monkeypatch.setattr(t, "_table_client", _client)

    page, cursor = t._list_table_page(2, "cursor")

    assert len(page) == 2
    assert cursor
    assert len(queries) == 1
    assert queries[0][1] == 2


# --------------------------------------------------------------------------- #
# Table backend: conditional-write etag contract
#
# ``azure-data-tables`` moves ``odata.etag`` into ``TableEntity.metadata`` during
# deserialization, so reading it off the mapping yields ``None`` and the SDK then
# raises ``ValueError: IfNotModified must be specified with etag.``. In
# production that escaped ``claim_bridge`` (message abandoned) and silently
# no-op'd ``release_bridge``, wedging the correlation id so its request was never
# submitted and no completion event was ever published. These tests pin the fix.
# --------------------------------------------------------------------------- #

_ETAG = "W/\"datetime'2026-08-05T06%3A15%3A31.0Z'\""


def _table_entity(
    correlation_id: str,
    *,
    etag: str | None = _ETAG,
    openapi_job_id: str = "",
) -> TableEntity:
    """A deserialized row whose etag lives in ``metadata``, as the SDK returns it."""
    record = BridgeRecord(
        correlation_id=correlation_id,
        openapi_job_id=openapi_job_id,
        # Far in the past → stale under any allowed threshold (floored at 30s).
        created_at="2020-01-01T00:00:00+00:00",
        claimed_at="2020-01-01T00:00:00+00:00",
    )
    entity = TableEntity(t._entity(record))
    entity._metadata = {"etag": etag, "timestamp": None}
    return entity


class _FakeTable:
    """Minimal TableClient stand-in that enforces the SDK's etag precondition."""

    def __init__(self, entity: TableEntity, *, delete_error: Exception | None = None) -> None:
        self.entity = entity
        self.delete_error = delete_error
        self.updates: list[str] = []
        self.deletes: list[str] = []

    def create_entity(self, entity: dict[str, Any]) -> None:
        raise ResourceExistsError("row already exists")

    def get_entity(self, *, partition_key: str, row_key: str) -> TableEntity:
        return self.entity

    def _require_etag(self, etag: Any) -> str:
        # Mirrors azure.data.tables._serialize._get_match_condition.
        if not etag:
            raise ValueError("IfNotModified must be specified with etag.")
        return str(etag)

    def update_entity(
        self, entity: dict[str, Any], *, mode: Any, etag: Any, match_condition: Any
    ) -> None:
        self.updates.append(self._require_etag(etag))

    def delete_entity(
        self, *, partition_key: str, row_key: str, etag: Any, match_condition: Any
    ) -> None:
        self._require_etag(etag)
        if self.delete_error is not None:
            raise self.delete_error
        self.deletes.append(str(etag))


@pytest.fixture
def _fake_table(monkeypatch: pytest.MonkeyPatch):
    def _install(
        entity: TableEntity, *, delete_error: Exception | None = None
    ) -> _FakeTable:
        table = _FakeTable(entity, delete_error=delete_error)

        @contextmanager
        def _client():
            yield table

        monkeypatch.setattr(t, "_ensure_table", lambda: None)
        monkeypatch.setattr(t, "_table_client", _client)
        return table

    return _install


def test_claim_table_steals_stale_reservation_using_metadata_etag(_fake_table) -> None:
    table = _fake_table(_table_entity("corr-steal"))

    # Pre-fix this raised ValueError out of claim_bridge, the drain handler
    # abandoned the message, and the correlation id stayed wedged forever.
    assert t._claim_table("corr-steal", "req-1", "fingerprint-1") is True
    assert table.updates == [_ETAG]


def test_claim_table_refuses_steal_when_etag_is_unavailable(_fake_table) -> None:
    table = _fake_table(_table_entity("corr-no-etag", etag=None))

    # No etag → the steal cannot be guarded by optimistic concurrency. Refuse
    # (defer this delivery) rather than risk two workers both submitting.
    assert t._claim_table("corr-no-etag", "req-1", "fingerprint-1") is False
    assert table.updates == []


def test_claim_table_does_not_steal_a_confirmed_row(_fake_table) -> None:
    table = _fake_table(_table_entity("corr-confirmed", openapi_job_id="job-1"))

    assert t._claim_table("corr-confirmed", "req-1", "fingerprint-1") is False
    assert table.updates == []


def test_release_table_deletes_unconfirmed_row_using_metadata_etag(_fake_table) -> None:
    table = _fake_table(_table_entity("corr-release"))

    # Pre-fix the conditional delete raised ValueError, was swallowed at DEBUG,
    # and left the phantom reservation behind.
    t._release_table("corr-release")
    assert table.deletes == [_ETAG]


def test_release_table_never_deletes_a_confirmed_row(_fake_table) -> None:
    table = _fake_table(_table_entity("corr-live", openapi_job_id="job-live"))

    t._release_table("corr-live")
    assert table.deletes == []


def test_entity_etag_prefers_metadata_over_mapping_key() -> None:
    entity = _table_entity("corr-etag")
    assert t._entity_etag(entity) == _ETAG
    # A deserialized entity never carries the odata key in the mapping itself.
    assert "odata.etag" not in dict(entity)
    # Plain-dict fallback stays supported for fixtures / non-SDK callers.
    assert t._entity_etag({"odata.etag": "W/\"x\""}) == "W/\"x\""
    assert t._entity_etag({}) == ""


def test_release_table_is_quiet_when_the_row_vanishes_mid_delete(
    _fake_table, caplog: pytest.LogCaptureFixture
) -> None:
    """A concurrent release removing the row first is a benign race.

    The conditional delete must swallow it exactly like ResourceModifiedError;
    letting it fall through to the catch-all would print a traceback at WARNING
    for a non-event and train operators to ignore that line.
    """
    table = _fake_table(_table_entity("corr-race"), delete_error=ResourceNotFoundError("gone"))

    with caplog.at_level("WARNING", logger=t.LOGGER.name):
        t._release_table("corr-race")

    assert table.deletes == []
    assert [r for r in caplog.records if r.name == t.LOGGER.name] == []


def test_release_table_logs_unexpected_failures_at_warning(
    _fake_table, caplog: pytest.LogCaptureFixture
) -> None:
    """The catch-all stays best-effort but must never be silent again.

    This path ran at DEBUG in production, which is exactly why the etag defect
    survived undetected until requests started disappearing.
    """
    table = _fake_table(_table_entity("corr-boom"), delete_error=RuntimeError("table down"))

    with caplog.at_level("WARNING", logger=t.LOGGER.name):
        t._release_table("corr-boom")

    assert table.deletes == []
    assert any("release_bridge (table) failed" in r.message for r in caplog.records)


def test_stale_claim_threshold_outlives_a_slow_sibling_submit() -> None:
    """The steal must never fire while the holder's submit can still be alive.

    Stealing a live reservation lets two workers submit the SAME correlation id,
    producing a duplicate BLAST run. Before the etag fix the steal path always
    raised, so this margin was never actually exercised in production — it is now,
    which is why it gets a regression guard instead of only a code comment. The
    guard pins the FLOOR (not just the default) because the threshold is operator
    tunable via SERVICEBUS_CLAIM_STALE_SECONDS.
    """
    from api.services import external_blast

    assert t._CLAIM_STALE_FLOOR_SECONDS > external_blast._DEFAULT_TIMEOUT_SECONDS
    assert t._claim_stale_seconds_from_env() >= t._CLAIM_STALE_FLOOR_SECONDS


def test_release_table_propagates_celery_soft_time_limit(_fake_table) -> None:
    """The best-effort catch-all must not swallow the task's soft time limit.

    Repo-wide contract: SoftTimeLimitExceeded means the task budget is spent, so
    swallowing it here would let the drain keep working past its limit.
    """
    _fake_table(
        _table_entity("corr-soft"), delete_error=SoftTimeLimitExceeded()
    )

    with pytest.raises(SoftTimeLimitExceeded):
        t._release_table("corr-soft")
