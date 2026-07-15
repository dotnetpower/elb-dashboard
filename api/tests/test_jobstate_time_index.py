"""Tests for the jobstate time-ordered secondary index (#50).

Responsibility: Cover the pure key/cursor helpers in ``time_index`` and the
flag-gated index maintenance + ``list_owner_page`` read path in
``JobStateRepository`` (create adds an immutable index row, soft-delete removes
it, the indexed read returns the most-recent N newest-first across the owner +
shared buckets with a round-trippable cursor, and an empty/failed index falls
back to the legacy scan).
Edit boundaries: In-memory fake Table — no live Azure. Keep the OData evaluator
limited to the operators the repository actually emits.
Key entry points: ``test_row_key_orders_newest_first``,
``test_create_writes_index_row_when_enabled``,
``test_list_owner_page_paginates_without_overlap``,
``test_soft_delete_removes_index_row``,
``test_list_for_owner_falls_back_when_index_empty``.
Risky contracts: The index key must stay immutable (owner_oid + created_at);
tests pin that a status update does NOT move the row.
Validation: ``uv run pytest -q api/tests/test_jobstate_time_index.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from api.services.state import repository as state_repo
from api.services.state import time_index
from api.services.state.repository import JobState, JobStateRepository
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_owner_bucket_maps_empty_to_shared() -> None:
    assert time_index.owner_bucket("oid-abc") == "oid-abc"
    assert time_index.owner_bucket("") == time_index.SHARED_BUCKET
    assert time_index.owner_bucket(None) == time_index.SHARED_BUCKET
    assert time_index.owner_bucket("  ") == time_index.SHARED_BUCKET


def test_row_key_orders_newest_first() -> None:
    older = time_index.row_key("2026-06-18T10:00:00+00:00", "job-old")
    newer = time_index.row_key("2026-06-18T10:00:05+00:00", "job-new")
    # Newer timestamp -> smaller inverted prefix -> sorts FIRST ascending.
    assert newer < older
    # Fixed-width prefix so lexical order == numeric order.
    assert older.split("_")[0].isdigit() and len(older.split("_")[0]) == 14


def test_row_key_handles_bad_timestamp() -> None:
    # An unparseable created_at must not raise; it sinks to the bottom (oldest).
    bad = time_index.row_key("not-a-date", "job-x")
    good = time_index.row_key("2026-06-18T10:00:00+00:00", "job-y")
    assert bad > good  # bad (epoch 0) sorts after a real recent timestamp


def test_cursor_round_trip_and_garbage() -> None:
    rk = time_index.row_key("2026-06-18T10:00:00+00:00", "job-1")
    assert time_index.decode_cursor(time_index.encode_cursor(rk)) == rk
    assert time_index.decode_cursor("") == ""
    assert time_index.decode_cursor("!!!not-base64!!!") == ""
    # Validly-encoded but not a RowKey shape -> rejected (fail closed).
    import base64

    bogus = base64.urlsafe_b64encode(b"PartitionKey eq 'x'").decode("ascii")
    assert time_index.decode_cursor(bogus) == ""


def test_time_index_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    assert time_index.time_index_enabled() is False
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    assert time_index.time_index_enabled() is True
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "0")
    assert time_index.time_index_enabled() is False


# ---------------------------------------------------------------------------
# In-memory fake Azure Table + tiny OData evaluator
# ---------------------------------------------------------------------------


def _split_top(expr: str, sep: str) -> list[str]:
    """Split ``expr`` on ``sep`` at paren-depth 0."""
    parts: list[str] = []
    depth = 0
    buf = ""
    i = 0
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth == 0 and expr[i : i + len(sep)] == sep:
            parts.append(buf)
            buf = ""
            i += len(sep)
            continue
        buf += c
        i += 1
    parts.append(buf)
    return parts


def _strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        wraps = True
        for idx, c in enumerate(expr):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and idx != len(expr) - 1:
                    wraps = False
                    break
        if wraps:
            expr = expr[1:-1].strip()
        else:
            break
    return expr


_ATOM_RE = re.compile(r"^(\w+)\s+(eq|ne|gt|lt|ge|le)\s+(.+)$")


def _atom(entity: dict[str, Any], atom: str) -> bool:
    atom = _strip_parens(atom)
    m = _ATOM_RE.match(atom)
    assert m is not None, f"unsupported atom: {atom!r}"
    field, op, val = m.group(1), m.group(2), m.group(3).strip()
    if val.startswith("'") and val.endswith("'"):
        val = val[1:-1]
    raw = entity.get(field)
    actual = "" if raw is None else str(raw)
    return {
        "eq": actual == val,
        "ne": actual != val,
        "gt": actual > val,
        "lt": actual < val,
        "ge": actual >= val,
        "le": actual <= val,
    }[op]


def _eval(entity: dict[str, Any], expr: str) -> bool:
    """Recursively evaluate the (limited) OData expressions the repo emits.

    Handles ``or`` / ``and`` (paren-aware, lowest precedence first) and falls
    through to a single ``field op 'value'`` atom — including the
    ``(owner_oid eq 'x' or owner_oid eq '')`` group nested inside an ``and``.
    """
    expr = _strip_parens(expr)
    or_parts = _split_top(expr, " or ")
    if len(or_parts) > 1:
        return any(_eval(entity, part) for part in or_parts)
    and_parts = _split_top(expr, " and ")
    if len(and_parts) > 1:
        return all(_eval(entity, part) for part in and_parts)
    return _atom(entity, expr)


def _match(entity: dict[str, Any], filt: str) -> bool:
    return _eval(entity, filt.strip())


class _FakeTable:
    def __init__(self, name: str, store: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.name = name
        self.rows = store
        self.fail_upsert = False
        self.closed = False
        # Counts rows YIELDED from query_entities so a test can assert the
        # indexed read path consumes at most ~limit rows (bounded scan).
        self.query_count = 0

    def __enter__(self) -> _FakeTable:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def close(self) -> None:
        # A real Table client closes its HTTP transport here; track it so a
        # caller that wrongly closes the POOLED (shared) client is caught when
        # the next operation runs (regression guard for the backfill footgun).
        self.closed = True

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError("operation on a closed Table client")

    def create_entity(self, entity: dict[str, Any]) -> None:
        self._check_open()
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError(message="exists")
        self.rows[key] = dict(entity)

    def upsert_entity(self, entity: dict[str, Any], **_kw: object) -> None:
        self._check_open()
        if self.fail_upsert:
            raise RuntimeError("simulated index write failure")
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def update_entity(self, entity: dict[str, Any], **_kw: object) -> None:
        key = (entity["PartitionKey"], entity["RowKey"])
        existing = self.rows.get(key, {})
        existing.update(entity)
        self.rows[key] = existing

    def delete_entity(self, partition_key: str = "", row_key: str = "", **_kw: object) -> None:
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError(message="missing")
        del self.rows[key]

    def get_entity(
        self, partition_key: str = "", row_key: str = "", **_kw: object
    ) -> dict[str, Any]:
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError(message="missing")
        return dict(self.rows[key])

    def query_entities(self, query_filter: str, **_kw: object) -> Iterator[dict[str, Any]]:
        # Azure Table Storage returns entities sorted by (PartitionKey, RowKey)
        # ascending; the indexed read path relies on that ordering, so the fake
        # must emulate it rather than returning dict-insertion order. Yields
        # lazily + counts so a caller that breaks at limit+1 only consumes that
        # many rows (the bounded-scan guarantee).
        matched = [dict(r) for r in self.rows.values() if _match(r, query_filter)]
        matched.sort(key=lambda e: (str(e.get("PartitionKey")), str(e.get("RowKey"))))
        for row in matched:
            self.query_count += 1
            yield row


class _FakeService:
    def __init__(self, **_kw: object) -> None:
        pass

    def __enter__(self) -> _FakeService:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def create_table_if_not_exists(self, _name: str) -> None:
        return None


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> JobStateRepository:
    """A repository wired to per-table in-memory fakes (jobstate + index)."""
    stores: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    tables: dict[str, _FakeTable] = {}

    def _table_client(
        *, endpoint: str = "", table_name: str = "", credential: object = None
    ) -> _FakeTable:
        store = stores.setdefault(table_name, {})
        table = tables.get(table_name)
        if table is None:
            table = _FakeTable(table_name, store)
            tables[table_name] = table
        return table

    monkeypatch.setenv("AZURE_TABLE_ENDPOINT", "https://acct.table.core.windows.net")
    monkeypatch.setattr(state_repo, "TableClient", _table_client)
    monkeypatch.setattr(state_repo, "TableServiceClient", _FakeService)
    monkeypatch.setattr(state_repo, "get_credential", lambda: object())
    state_repo._ENSURED_TABLES.clear()

    r = JobStateRepository()
    # Expose the fakes for assertions / fault injection.
    r._test_tables = tables  # type: ignore[attr-defined]
    return r


def _job(job_id: str, *, owner: str, created_at: str) -> JobState:
    return JobState(
        job_id=job_id,
        type="blast",
        status="queued",
        owner_oid=owner,
        created_at=created_at,
    )


def test_create_writes_index_row_when_enabled(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))

    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    expected_rk = time_index.row_key("2026-06-18T10:00:00+00:00", "job-1")
    assert ("owner-a", expected_rk) in index.rows
    assert index.rows[("owner-a", expected_rk)]["job_id"] == "job-1"


def test_create_writes_no_index_row_when_disabled(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    assert time_index.INDEX_TABLE_NAME not in repo._test_tables  # type: ignore[attr-defined]


def test_index_write_failure_does_not_fail_create(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    # Prime the index table then make its upsert fail (partial-failure path:
    # the jobstate row is written, the index write fails).
    repo._ensure_table(time_index.INDEX_TABLE_NAME)
    repo._index_client()  # construct the pooled client / fake
    repo._test_tables[time_index.INDEX_TABLE_NAME].fail_upsert = True  # type: ignore[attr-defined]

    created = repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    # Create still succeeds (row is the source of truth); index row is absent.
    assert created.job_id == "job-1"
    assert repo.get("job-1") is not None
    assert ("owner-a", time_index.row_key("2026-06-18T10:00:00+00:00", "job-1")) not in (
        repo._test_tables[time_index.INDEX_TABLE_NAME].rows  # type: ignore[attr-defined]
    )


def test_list_owner_page_paginates_without_overlap(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    for i in range(5):
        repo.create(_job(f"job-{i}", owner="owner-a", created_at=f"2026-06-18T10:00:0{i}+00:00"))

    page1, cursor1 = repo.list_owner_page("owner-a", limit=2)
    assert [s.job_id for s in page1] == ["job-4", "job-3"]  # newest first
    assert cursor1 is not None

    page2, cursor2 = repo.list_owner_page("owner-a", limit=2, cursor=cursor1)
    assert [s.job_id for s in page2] == ["job-2", "job-1"]
    assert cursor2 is not None

    page3, cursor3 = repo.list_owner_page("owner-a", limit=2, cursor=cursor2)
    assert [s.job_id for s in page3] == ["job-0"]
    assert cursor3 is None  # exhausted -> no further cursor

    # No overlap / no gaps across the three pages.
    seen = [s.job_id for s in page1 + page2 + page3]
    assert seen == ["job-4", "job-3", "job-2", "job-1", "job-0"]


def test_list_owner_page_merges_owner_and_shared_buckets(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("owned", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("shared", owner="", created_at="2026-06-18T10:00:03+00:00"))
    repo.create(_job("other", owner="owner-b", created_at="2026-06-18T10:00:09+00:00"))

    rows, _cursor = repo.list_owner_page("owner-a", limit=10)
    ids = [s.job_id for s in rows]
    # owner-a sees its own row + the shared row, newest first; never owner-b's.
    assert ids == ["shared", "owned"]


def test_soft_delete_removes_index_row(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("job-2", owner="owner-a", created_at="2026-06-18T10:00:01+00:00"))

    repo.update("job-1", status="deleted", phase="deleted")

    rk = time_index.row_key("2026-06-18T10:00:00+00:00", "job-1")
    assert ("owner-a", rk) not in repo._test_tables[time_index.INDEX_TABLE_NAME].rows  # type: ignore[attr-defined]

    rows, _cursor = repo.list_owner_page("owner-a", limit=10)
    assert [s.job_id for s in rows] == ["job-2"]


def test_status_update_does_not_move_index_row(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-delete status transition must NOT touch the index (immutable key)."""
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    rk = time_index.row_key("2026-06-18T10:00:00+00:00", "job-1")
    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    assert ("owner-a", rk) in index.rows

    repo.update("job-1", status="running")
    repo.update("job-1", status="completed")
    # Same single index row, same key — never moved or duplicated.
    assert [k for k in index.rows if k[0] == "owner-a"] == [("owner-a", rk)]


def test_idempotent_create_keeps_single_index_row(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    # A re-create with the same job_id (concurrent-create race) upserts the
    # SAME index RowKey, so there is never a duplicate index row.
    repo._index_put(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    assert len([k for k in index.rows if k[0] == "owner-a"]) == 1


def test_list_for_owner_uses_index_when_enabled(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    for i in range(3):
        repo.create(_job(f"job-{i}", owner="owner-a", created_at=f"2026-06-18T10:00:0{i}+00:00"))
    rows = repo.list_for_owner("owner-a", limit=2)
    assert [s.job_id for s in rows] == ["job-2", "job-1"]


def test_list_for_owner_falls_back_when_index_empty(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag ON but jobs were created BEFORE the index existed (un-backfilled):
    the indexed read returns nothing, so list_for_owner falls back to the legacy
    scan rather than hiding the jobs."""
    # Create with the flag OFF so no index rows are written.
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-legacy", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))

    # Now flip the flag ON; the index table is empty for this owner.
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    rows = repo.list_for_owner("owner-a", limit=10)
    assert [s.job_id for s in rows] == ["job-legacy"]


# ---------------------------------------------------------------------------
# Global __all__ bucket: list_all indexed page (#50)
# ---------------------------------------------------------------------------


def test_create_writes_all_bucket_row_when_enabled(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create fans the index row into BOTH the owner bucket and __all__."""
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    rk = time_index.row_key("2026-06-18T10:00:00+00:00", "job-1")
    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    assert ("owner-a", rk) in index.rows
    assert (time_index.ALL_BUCKET, rk) in index.rows


def test_list_all_page_paginates_without_overlap(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_all_page reads the single __all__ bucket newest-first across owners
    with a round-trippable cursor and no overlap/gaps."""
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    # Mixed owners — list_all is owner-agnostic.
    repo.create(_job("job-0", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("job-1", owner="", created_at="2026-06-18T10:00:01+00:00"))
    repo.create(_job("job-2", owner="owner-b", created_at="2026-06-18T10:00:02+00:00"))
    repo.create(_job("job-3", owner="owner-a", created_at="2026-06-18T10:00:03+00:00"))
    repo.create(_job("job-4", owner="owner-c", created_at="2026-06-18T10:00:04+00:00"))

    page1, cursor1 = repo.list_all_page(limit=2)
    assert [s.job_id for s in page1] == ["job-4", "job-3"]  # newest first, any owner
    assert cursor1 is not None

    page2, cursor2 = repo.list_all_page(limit=2, cursor=cursor1)
    assert [s.job_id for s in page2] == ["job-2", "job-1"]
    assert cursor2 is not None

    page3, cursor3 = repo.list_all_page(limit=2, cursor=cursor2)
    assert [s.job_id for s in page3] == ["job-0"]
    assert cursor3 is None  # exhausted

    seen = [s.job_id for s in page1 + page2 + page3]
    assert seen == ["job-4", "job-3", "job-2", "job-1", "job-0"]


def test_list_all_page_bounded_scan(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_all_page reads at most ~limit rows from the index, never the full set
    (the AC1 'scan size is bounded' guarantee for list_all)."""
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    for i in range(8):
        repo.create(_job(f"job-{i}", owner="owner-a", created_at=f"2026-06-18T10:00:0{i}+00:00"))
    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    index.query_count = 0  # type: ignore[attr-defined]
    rows, _cursor = repo.list_all_page(limit=2)
    assert [s.job_id for s in rows] == ["job-7", "job-6"]
    # The index query stopped at limit+1 rows, NOT all 8.
    assert index.query_count <= 3  # type: ignore[attr-defined]


def test_list_all_uses_index_when_enabled(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("job-0", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("job-1", owner="owner-b", created_at="2026-06-18T10:00:01+00:00"))
    rows = repo.list_all(limit=10)
    assert [s.job_id for s in rows] == ["job-1", "job-0"]


def test_list_all_falls_back_when_index_empty(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag ON but jobs predate the index (un-backfilled): list_all falls back to
    the legacy scan rather than hiding jobs."""
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-legacy", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    rows = repo.list_all(limit=10)
    assert [s.job_id for s in rows] == ["job-legacy"]


def test_list_for_scope_filters_mutable_scope_over_global_index(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutable scope values are filtered from current rows over the global index."""
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("scoped", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    # cluster_name is backfilled AFTER create (mutable) — exactly why a scope
    # index can't use the immutable key.
    repo.update("scoped", cluster_name="elb-cluster-01")
    rows = repo.list_for_scope(cluster_name="elb-cluster-01", limit=10)
    assert [s.job_id for s in rows] == ["scoped"]


def test_list_for_scope_index_walks_past_newer_nonmatching_rows(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    repo.create(_job("matching-old", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.update("matching-old", subscription_id="sub-target")
    for i in range(150):
        job_id = f"other-{i:03d}"
        repo.create(
            _job(
                job_id,
                owner="owner-b",
                created_at=f"2026-06-19T10:{i // 60:02d}:{i % 60:02d}+00:00",
            )
        )
        repo.update(job_id, subscription_id="sub-other")

    rows = repo.list_for_scope(subscription_id="sub-target", limit=10)

    assert [s.job_id for s in rows] == ["matching-old"]


def test_list_for_scope_empty_index_preserves_legacy_fallback(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("legacy", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.update("legacy", subscription_id="sub-target")
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")

    rows = repo.list_for_scope(subscription_id="sub-target", limit=10)

    assert [s.job_id for s in rows] == ["legacy"]


# ---------------------------------------------------------------------------
# Backfill migration (scripts/dev/backfill_jobstate_time_index.py)
# ---------------------------------------------------------------------------


def _load_backfill_module() -> Any:
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "dev" / "backfill_jobstate_time_index.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_jobstate_time_index", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_dry_run_writes_nothing_then_real_run_is_idempotent(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backfill scans non-deleted jobstate rows and upserts one index row
    each (skipping tombstones); --dry-run writes nothing; a second real run is
    idempotent (same RowKey per job -> no duplicates)."""
    # Seed jobstate with the flag OFF so create writes NO index rows (simulating
    # pre-existing rows that predate the feature).
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-0", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:01+00:00"))
    repo.create(_job("job-2", owner="", created_at="2026-06-18T10:00:02+00:00"))  # shared
    repo.create(_job("job-del", owner="owner-a", created_at="2026-06-18T10:00:03+00:00"))
    repo.update("job-del", status="deleted", phase="deleted")  # tombstone -> excluded

    # The backfill resolves its repo via get_state_repo(); reset the cache so it
    # builds a fresh repo wired to the SAME patched fakes.
    state_repo.reset_state_repo_cache()
    backfill = _load_backfill_module()

    # --- dry run: scans, writes nothing ---
    rc = backfill.backfill(dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN done scanned=3 backfilled=3" in out
    # No index table touched in dry-run.
    assert time_index.INDEX_TABLE_NAME not in repo._test_tables  # type: ignore[attr-defined]

    # --- real run: upserts one index row per non-deleted job ---
    rc = backfill.backfill(dry_run=False)
    assert rc == 0
    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    keys = set(index.rows.keys())
    rk0 = time_index.row_key("2026-06-18T10:00:00+00:00", "job-0")
    rk1 = time_index.row_key("2026-06-18T10:00:01+00:00", "job-1")
    shared_rk = time_index.row_key("2026-06-18T10:00:02+00:00", "job-2")
    assert ("owner-a", rk0) in keys
    assert ("owner-a", rk1) in keys
    assert (time_index.SHARED_BUCKET, shared_rk) in keys
    # Each non-deleted job is ALSO indexed into the global __all__ bucket (#50)
    # so list_all is a bounded single-partition read.
    assert (time_index.ALL_BUCKET, rk0) in keys
    assert (time_index.ALL_BUCKET, rk1) in keys
    assert (time_index.ALL_BUCKET, shared_rk) in keys
    # The deleted job is NOT indexed (in any bucket).
    assert all("job-del" not in rk for _pk, rk in keys)
    assert len(keys) == 6  # 3 non-deleted jobs x (owner/shared + __all__)

    # --- idempotent: a second run upserts the same RowKeys, no duplicates ---
    backfill.backfill(dry_run=False)
    index_again = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    assert set(index_again.rows.keys()) == keys
    assert len(index_again.rows) == 6


# ---------------------------------------------------------------------------
# Periodic reconcile task (api.tasks.blast.reconcile_time_index, #50)
# ---------------------------------------------------------------------------


def test_reconcile_time_index_method_dry_run_touches_no_table(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo method's dry-run counts rows but never creates the index table."""
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-0", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("job-1", owner="owner-a", created_at="2026-06-18T10:00:01+00:00"))

    scanned, written = repo.reconcile_time_index(dry_run=True)
    assert (scanned, written) == (2, 2)
    assert time_index.INDEX_TABLE_NAME not in repo._test_tables  # type: ignore[attr-defined]


def test_reconcile_time_index_task_noop_when_flag_off(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag OFF: the task returns early, creates no index table, writes nothing."""
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-0", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))

    from api.tasks.blast import reconcile_time_index

    result = reconcile_time_index.run()
    assert result == {"skipped": "flag_off", "scanned": 0, "written": 0}
    assert time_index.INDEX_TABLE_NAME not in repo._test_tables  # type: ignore[attr-defined]


def test_reconcile_time_index_task_heals_missing_rows_when_flag_on(
    repo: JobStateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag ON but rows were created un-indexed (best-effort ``_index_put``
    missed them): the reconcile task re-adds one index row per non-deleted job,
    skips tombstones, and is idempotent on a second pass."""
    # Seed with the flag OFF so create writes NO index rows (the un-indexed gap a
    # transient _index_put failure would leave).
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    repo.create(_job("job-0", owner="owner-a", created_at="2026-06-18T10:00:00+00:00"))
    repo.create(_job("job-1", owner="", created_at="2026-06-18T10:00:01+00:00"))  # shared
    repo.create(_job("job-del", owner="owner-a", created_at="2026-06-18T10:00:02+00:00"))
    repo.update("job-del", status="deleted", phase="deleted")  # tombstone -> excluded

    # The task resolves its repo via get_state_repo(); reset so it builds a fresh
    # repo wired to the SAME patched fakes.
    state_repo.reset_state_repo_cache()
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")

    from api.tasks.blast import reconcile_time_index

    result = reconcile_time_index.run()
    assert result == {"scanned": 2, "written": 2}

    index = repo._test_tables[time_index.INDEX_TABLE_NAME]  # type: ignore[attr-defined]
    keys = set(index.rows.keys())
    rk0 = time_index.row_key("2026-06-18T10:00:00+00:00", "job-0")
    shared_rk = time_index.row_key("2026-06-18T10:00:01+00:00", "job-1")
    assert ("owner-a", rk0) in keys
    assert (time_index.SHARED_BUCKET, shared_rk) in keys
    # Both jobs are ALSO healed into the global __all__ bucket (#50).
    assert (time_index.ALL_BUCKET, rk0) in keys
    assert (time_index.ALL_BUCKET, shared_rk) in keys
    assert all("job-del" not in rk for _pk, rk in keys)
    assert len(keys) == 4  # 2 non-deleted jobs x (owner/shared + __all__)

    # Idempotent: a second pass writes the same RowKeys, no duplicates.
    result2 = reconcile_time_index.run()
    assert result2 == {"scanned": 2, "written": 2}
    assert set(repo._test_tables[time_index.INDEX_TABLE_NAME].rows.keys()) == keys  # type: ignore[attr-defined]
