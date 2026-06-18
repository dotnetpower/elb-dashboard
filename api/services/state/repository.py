"""`JobStateRepository` — Azure Table-backed jobstate + jobhistory access.

Responsibility: All read/write operations against the `jobstate` and
`jobhistory` Azure Tables on the platform Storage account, plus the
module-level singleton getter that hot routes reuse so each request
does not pay another TLS handshake.
Edit boundaries: Azure-Tables SDK lives here. Domain shaping
(JobState / canonical metadata) lives in `job_state.py`. Connection
pool primitive lives in `table_pool.py`.
Key entry points: `JobStateRepository`, `get_state_repo`, `reset_state_repo_cache`.
Risky contracts: Every OData filter MUST flow through `_sanitise_odata_value`.
Validation: `uv run pytest -q api/tests/test_state_repo.py`.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import os
import threading
from datetime import datetime
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential
from api.services.state.job_state import (
    _JOB_SCHEMA_VERSION,
    _JOBSTATE_SUMMARY_SELECT,
    JobState,
    _now_iso,
    _sanitise_odata_value,
    _ulid_like,
    canonical_job_metadata,
)
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

LOGGER = logging.getLogger(__name__)

# Azure Table Storage hard-caps `$top` (page size) at 1000 entities per
# response. Asking for more returns HTTP 400
# ``InvalidInput / "One of the request inputs is not valid"`` with no
# results, which silently breaks any caller that passes a larger logical
# ``limit`` straight through to ``results_per_page`` (the cancel task hit
# this with ``limit=10_000`` and stalled the cluster card on "Running"
# because it never got past ``list_children``).
#
# The SDK's iterator handles multi-page walks transparently, so callers
# can still ask for a logical limit > 1000 — we just have to clamp the
# per-request page size and let pagination do its job.
_AZURE_TABLES_MAX_PAGE_SIZE = 1000


def _clamp_page_size(limit: int) -> int:
    if limit <= 0:
        return 1
    return min(limit, _AZURE_TABLES_MAX_PAGE_SIZE)


# Hard cap on how many rows a user-facing "most recent N" listing
# (`list_for_owner` / `list_all` / `list_for_scope`) will scan before
# sorting. Azure Table Storage has no server-side ordering and jobstate rows
# use a random-uuid PartitionKey, so the only way to return the genuinely
# most-recent `limit` rows is to read the full filtered set and sort it in
# process. This cap bounds the memory / latency of that scan; beyond it the
# ordering is best-effort and a time-ordered secondary index would be the
# proper fix (logged at WARNING when hit). Override with
# ``JOBSTATE_LIST_SCAN_CAP``.
_LIST_SCAN_HARD_CAP_DEFAULT = 5000

# ---------------------------------------------------------------------------
# Secondary index: ``jobstateidx`` table
# ---------------------------------------------------------------------------
# Rows use  PartitionKey=owner_oid, RowKey="{inverted_epoch_ms:013d}_{job_id}"
# so a single results_per_page=N read returns the N newest rows in order
# (Azure Table Storage returns ascending RowKey; lower inverted epoch = newer
# job). The index is written on create, refreshed on update, deleted on
# soft-delete.  list_for_owner_indexed uses it; list_for_owner falls back to
# _list_recent_sorted when the index read raises.
_JOBSTATEIDX_TABLE = "jobstateidx"
_IDX_EPOCH_OFFSET = 10**13  # ms; safe until ~year 2286

# Select list for index reads: all summary fields that can be stored in the
# index entity, plus the ``job_id`` column that maps back to the main table's
# PartitionKey.
_JOBSTATEIDX_SELECT = [
    f for f in _JOBSTATE_SUMMARY_SELECT if f not in ("PartitionKey", "RowKey")
] + ["PartitionKey", "RowKey", "job_id"]
_JOBSTATE_SUMMARY_SELECT_SET = frozenset(_JOBSTATE_SUMMARY_SELECT) - {"PartitionKey", "RowKey"}


def _idx_row_key(created_at: str, job_id: str) -> str:
    """Inverted-time RowKey for newest-first ordering within an index partition.

    Azure Table Storage returns rows in ascending RowKey order within a
    partition.  By storing ``(_IDX_EPOCH_OFFSET - epoch_ms)`` as the leading
    segment, the row with the *smallest* inverted value (= most recent job)
    comes first in a plain ascending scan.  The job_id suffix makes keys unique
    when two jobs share the same millisecond timestamp.
    """
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        epoch_ms = int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        epoch_ms = 0
    inverted = max(0, _IDX_EPOCH_OFFSET - epoch_ms)
    return f"{inverted:013d}_{job_id}"


def _parse_idx_cursor(cursor: str | None) -> dict[str, str | None]:
    """Decode a base64-url JSON cursor returned by a previous indexed read.

    Returns ``{"o": owner_after, "s": shared_after}`` where each value is the
    last RowKey consumed from its partition (used as ``RowKey gt <value>`` on
    the next query).  Invalid or empty cursors silently return start-of-page.
    """
    if not cursor:
        return {"o": None, "s": None}
    try:
        # Restore stripped padding before decode.
        raw = base64.urlsafe_b64decode(cursor + "==").decode()
        parsed = _json.loads(raw)
        return {"o": parsed.get("o") or None, "s": parsed.get("s") or None}
    except Exception:
        LOGGER.warning("jobstateidx: ignoring invalid cursor %r", cursor[:80])
        return {"o": None, "s": None}


def _encode_idx_cursor(*, owner: str | None, shared: str | None) -> str:
    """Encode per-partition continuation RowKeys into an opaque cursor string."""
    payload: dict[str, str] = {}
    if owner:
        payload["o"] = owner
    if shared:
        payload["s"] = shared
    return base64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()


def _idx_entity_to_job_state(e: dict[str, Any]) -> JobState:
    """Convert an index entity to a JobState.

    Index rows use PartitionKey=owner_oid and RowKey=inverted_time_job_id.
    The actual job_id is preserved in the ``job_id`` field.  We build a
    synthetic main-table entity so the canonical ``JobState.from_entity``
    constructor works without changes.
    """
    synthetic = dict(e)
    synthetic["PartitionKey"] = e.get("job_id", "")
    synthetic["RowKey"] = "current"
    return JobState.from_entity(synthetic)


def _merge_idx_rows(
    owner: list[dict[str, Any]],
    shared: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Merge two RowKey-ascending index entity lists, returning at most *limit* rows.

    Both input lists are already sorted ascending by RowKey (smallest = newest
    first in inverted-time scheme) by the Azure Table Storage engine.  A
    standard two-pointer merge preserves that order without a full in-memory
    sort.
    """
    result: list[dict[str, Any]] = []
    i = j = 0
    while len(result) < limit:
        has_owner = i < len(owner)
        has_shared = j < len(shared)
        if not has_owner and not has_shared:
            break
        if has_owner and has_shared:
            if owner[i]["RowKey"] <= shared[j]["RowKey"]:
                result.append(owner[i])
                i += 1
            else:
                result.append(shared[j])
                j += 1
        elif has_owner:
            result.append(owner[i])
            i += 1
        else:
            result.append(shared[j])
            j += 1
    return result


def _list_scan_hard_cap() -> int:
    raw = os.environ.get("JOBSTATE_LIST_SCAN_CAP", "")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return _LIST_SCAN_HARD_CAP_DEFAULT
        if value > 0:
            return value
    return _LIST_SCAN_HARD_CAP_DEFAULT


# Keys whose value is an Entra OID, UPN, or email address. Used by the
# `STRICT_AUDIT_HASH` gate to hash PII out of `jobhistory.payload_json`
# at write time. Lower-cased substrings — matched with `.endswith()` or
# exact equality so e.g. `caller_oid`, `owner_oid`, `actor_oid`, and a
# bare `oid` key all qualify, but unrelated keys (`void`, `paranoid`,
# `cosmosdb_resource_id`) don't.
_PII_KEY_EXACT = frozenset({
    "oid",
    "upn",
    "email",
    "actor",
    "principal",
    "principal_id",
    "object_id",
    "preferred_username",
    "user_id",
    "userid",
})
_PII_KEY_SUFFIXES = ("_oid", "_upn", "_email", "_actor")


def _is_pii_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _PII_KEY_EXACT:
        return True
    return lowered.endswith(_PII_KEY_SUFFIXES)


def _redact_audit_payload(payload: Any) -> Any:
    """Return a deep copy of `payload` with PII-bearing values hashed.

    Uses `api.services.sanitise.redact_oid` for the actual hashing so
    the format (sha256[:12]) matches the log redaction policy. Walks
    nested dicts and lists; leaves other types untouched.
    """
    from api.services.sanitise import redact_oid

    if isinstance(payload, dict):
        return {
            k: (
                redact_oid(str(v)) if _is_pii_key(k) and v is not None and v != ""
                else _redact_audit_payload(v)
            )
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_audit_payload(item) for item in payload]
    return payload


class JobStateRepository:
    """Read/write jobstate + jobhistory tables on the platform Storage account."""

    def __init__(self, table_endpoint: str | None = None):
        endpoint = table_endpoint or os.environ.get(_TABLE_ENDPOINT_ENV, "")
        if not endpoint:
            raise RuntimeError(
                f"{_TABLE_ENDPOINT_ENV} is not set. Set it to "
                "https://<account>.table.core.windows.net"
            )
        self._endpoint = endpoint
        self._cred = get_credential()
        # Cached pooled clients — lazily constructed on first use so a route
        # that only touches one table never pays for the other's pipeline.
        # Reused across all ``with self._state_client() as t:`` calls on this
        # instance so the TLS session and HTTP pipeline are shared per-repo.
        self._state_pool: _PooledTableClient | None = None
        self._history_pool: _PooledTableClient | None = None
        self._idx_pool: _PooledTableClient | None = None
        self._pool_lock = threading.Lock()

    def _state_client(self) -> _PooledTableClient:
        pool = self._state_pool
        if pool is None:
            with self._pool_lock:
                pool = self._state_pool
                if pool is None:
                    pool = _PooledTableClient(
                        TableClient(
                            endpoint=self._endpoint,
                            table_name="jobstate",
                            credential=self._cred,
                        )
                    )
                    self._state_pool = pool
        return pool

    def _history_client(self) -> _PooledTableClient:
        pool = self._history_pool
        if pool is None:
            with self._pool_lock:
                pool = self._history_pool
                if pool is None:
                    pool = _PooledTableClient(
                        TableClient(
                            endpoint=self._endpoint,
                            table_name="jobhistory",
                            credential=self._cred,
                        )
                    )
                    self._history_pool = pool
        return pool

    def _idx_client(self) -> _PooledTableClient:
        pool = self._idx_pool
        if pool is None:
            with self._pool_lock:
                pool = self._idx_pool
                if pool is None:
                    pool = _PooledTableClient(
                        TableClient(
                            endpoint=self._endpoint,
                            table_name=_JOBSTATEIDX_TABLE,
                            credential=self._cred,
                        )
                    )
                    self._idx_pool = pool
        return pool

    def _write_idx_entry(self, owner_oid: str, state: JobState) -> None:
        """Upsert a secondary index entry for *state*.

        Best-effort: a failure logs a warning but does NOT raise.  A backfill
        script can repair missed entries; the main-table row is the source of
        truth.  Uses REPLACE (not MERGE) so a re-write on update always reflects
        the current summary without partial stale fields.
        """
        if not state.job_id or not state.created_at:
            return
        rk = _idx_row_key(state.created_at, state.job_id)
        raw = state.to_entity()
        entity: dict[str, Any] = {k: v for k, v in raw.items() if k in _JOBSTATE_SUMMARY_SELECT_SET}
        entity["PartitionKey"] = owner_oid
        entity["RowKey"] = rk
        entity["job_id"] = state.job_id
        try:
            self._ensure_table(_JOBSTATEIDX_TABLE)
            with self._idx_client() as t:
                t.upsert_entity(entity, mode=UpdateMode.REPLACE)
        except Exception as exc:
            LOGGER.warning(
                "jobstateidx write failed for job_id=%s owner=%r: %s",
                state.job_id, owner_oid, exc,
            )

    def _delete_idx_entry(self, owner_oid: str, job_id: str, created_at: str) -> None:
        """Delete a secondary index entry on soft-delete.  Best-effort."""
        if not job_id or not created_at:
            return
        rk = _idx_row_key(created_at, job_id)
        try:
            with self._idx_client() as t:
                t.delete_entity(partition_key=owner_oid, row_key=rk)
        except ResourceNotFoundError:
            pass  # already absent — idempotent
        except Exception as exc:
            LOGGER.warning(
                "jobstateidx delete failed for job_id=%s owner=%r: %s",
                job_id, owner_oid, exc,
            )

    def _query_idx_partition(
        self,
        partition_key: str,
        after_row_key: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to *limit* index entities from one PartitionKey, newest-first.

        ``after_row_key`` (when set) acts as a cursor continuation: only rows
        with ``RowKey > after_row_key`` are returned, which is equivalent to
        "skip to the next page" in an ascending (newest-first) scan.
        """
        safe_pk = _sanitise_odata_value(partition_key)
        flt = f"PartitionKey eq '{safe_pk}'"
        if after_row_key:
            safe_rk = _sanitise_odata_value(after_row_key)
            flt += f" and RowKey gt '{safe_rk}'"
        rows: list[dict[str, Any]] = []
        try:
            with self._idx_client() as t:
                for e in t.query_entities(
                    flt,
                    results_per_page=_clamp_page_size(limit),
                    select=_JOBSTATEIDX_SELECT,
                ):
                    rows.append(dict(e))
                    if len(rows) >= limit:
                        break
        except ResourceNotFoundError:
            self._ensure_table(_JOBSTATEIDX_TABLE)
        return rows

    def list_for_owner_indexed(
        self,
        owner_oid: str,
        limit: int = 50,
        *,
        cursor: str | None = None,
    ) -> tuple[list[JobState], str | None, bool]:
        """Return ``(rows, next_cursor, has_more)`` from the time-ordered secondary index.

        Reads at most ``limit+1`` rows from the owner partition (PartitionKey =
        *owner_oid*) and the shared-jobs partition (PartitionKey = ''), merges
        them newest-first by RowKey, and builds an opaque cursor for the next
        page.

        Raises any unexpected exception so the caller can fall back to the
        legacy full-scan path (``_list_recent_sorted``).
        """
        parsed = _parse_idx_cursor(cursor)
        owner_after = parsed["o"]
        shared_after = parsed["s"]
        safe_oid = _sanitise_odata_value(owner_oid)
        fetch_limit = limit + 1

        owner_rows = self._query_idx_partition(safe_oid, owner_after, fetch_limit)
        shared_rows: list[dict[str, Any]] = []
        if safe_oid != "":
            # Only query shared partition separately; if the caller IS the shared
            # partition (owner_oid == "") we'd double-count the same rows.
            shared_rows = self._query_idx_partition("", shared_after, fetch_limit)

        merged = _merge_idx_rows(owner_rows, shared_rows, fetch_limit)
        has_more = len(merged) > limit
        page = merged[:limit]

        # Compute per-partition continuation cursors from the last row consumed
        # in this page.  Advance only the partition that supplied rows; keep the
        # prior cursor for the one that didn't (so the next page resumes from
        # the correct position in both streams).
        next_cursor: str | None = None
        if has_more and page:
            last_owner_rk: str | None = None
            last_shared_rk: str | None = None
            for row in page:
                if row.get("PartitionKey") == safe_oid:
                    last_owner_rk = row["RowKey"]
                else:
                    last_shared_rk = row["RowKey"]
            next_cursor = _encode_idx_cursor(
                owner=last_owner_rk if last_owner_rk is not None else owner_after,
                shared=last_shared_rk if last_shared_rk is not None else shared_after,
            )

        return [_idx_entity_to_job_state(e) for e in page], next_cursor, has_more

    def _ensure_table(self, table_name: str) -> None:
        key = (self._endpoint, table_name)
        if key in _ENSURED_TABLES:
            return
        # Double-checked locking — concurrent first requests previously
        # all saw an empty set and each fired their own TableServiceClient
        # + ``create_table_if_not_exists`` round-trip (idempotent on the
        # Azure side but wasteful TLS handshakes locally). The lock
        # collapses the storm into one client per (endpoint, table).
        with _ENSURED_TABLES_LOCK:
            if key in _ENSURED_TABLES:
                return
            with TableServiceClient(endpoint=self._endpoint, credential=self._cred) as service:
                try:
                    service.create_table_if_not_exists(table_name)
                except AttributeError:
                    try:
                        service.create_table(table_name)
                    except ResourceExistsError:
                        pass
            _ENSURED_TABLES.add(key)

    # --- jobstate ---

    def create(self, state: JobState) -> JobState:
        if not state.created_at:
            state.created_at = _now_iso()
        state.updated_at = state.created_at
        entity = state.to_entity()
        self._ensure_table("jobstate")
        try:
            with self._state_client() as t:
                t.create_entity(entity)
        except ResourceExistsError:
            # Concurrent create raced us. Return the row already on disk
            # rather than raising — sync callers can safely retry idempotently.
            existing = self.get(state.job_id)
            if existing is not None:
                existing._created_by_create = False
                return existing
            raise
        created = JobState.from_entity(entity)
        created._created_by_create = True
        self.append_history(
            created.job_id,
            "created",
            {"status": created.status, "phase": created.phase, "job_title": created.job_title},
        )
        # Secondary index: best-effort; a failure does NOT roll back the main write.
        if created.owner_oid:
            self._write_idx_entry(created.owner_oid, created)
        return created

    def get(self, job_id: str) -> JobState | None:
        with self._state_client() as t:
            try:
                e = t.get_entity(partition_key=job_id, row_key="current")
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
                return None
        return JobState.from_entity(dict(e))

    def get_summary(self, job_id: str) -> JobState | None:
        """Return a job row without the large ``payload_json`` property."""
        with self._state_client() as t:
            try:
                e = t.get_entity(
                    partition_key=job_id,
                    row_key="current",
                    select=_JOBSTATE_SUMMARY_SELECT,
                )
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
                return None
        return JobState.from_entity(dict(e))

    def find_by_task_id(self, task_id: str) -> JobState | None:
        """Return the JobState that recorded ``task_id`` as its Celery task, or None.

        Used by ownership-aware status endpoints (``/api/operations/{id}``,
        ``/api/tasks/{id}``) so the route can verify the caller owns the
        job before exposing the task result. Returns the summary row (no
        ``payload_json``) — the caller only needs ``owner_oid`` for the
        authorization check.
        """
        if not task_id:
            return None
        safe_id = _sanitise_odata_value(task_id)
        with self._state_client() as t:
            try:
                entities = t.query_entities(
                    f"task_id eq '{safe_id}' and RowKey eq 'current'",
                    select=_JOBSTATE_SUMMARY_SELECT,
                    results_per_page=1,
                )
                for e in entities:
                    return JobState.from_entity(dict(e))
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        return None

    def get_many(self, job_ids: list[str]) -> dict[str, JobState]:
        """Batch lookup for N job_ids using a single OData query.

        Returns a dict mapping job_id -> JobState for rows that exist.
        Missing job_ids are simply absent from the result.

        OData filter length limit is generous (~8 KB), and `limit` on the
        list route is capped at 500. A 12-char job_id contributes ~55 bytes
        to the filter, so 500 ids stay well under the limit.
        """
        if not job_ids:
            return {}
        # De-duplicate while preserving stable order for the query string.
        seen: set[str] = set()
        unique_ids: list[str] = []
        for jid in job_ids:
            if jid and jid not in seen:
                seen.add(jid)
                unique_ids.append(jid)
        if not unique_ids:
            return {}
        parts = [
            f"(PartitionKey eq '{_sanitise_odata_value(jid)}' and RowKey eq 'current')"
            for jid in unique_ids
        ]
        filter_expr = " or ".join(parts)
        result: dict[str, JobState] = {}
        with self._state_client() as t:
            try:
                for e in t.query_entities(filter_expr, results_per_page=len(unique_ids)):
                    state = JobState.from_entity(dict(e))
                    result[state.job_id] = state
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        return result

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        task_id: str | None = None,
        error_code: str | None = None,
        payload: dict[str, Any] | None = None,
        updated_at: str | None = None,
        subscription_id: str | None = None,
        resource_group: str | None = None,
        cluster_name: str | None = None,
        storage_account: str | None = None,
        job_title: str | None = None,
        program: str | None = None,
        db: str | None = None,
        query_label: str | None = None,
    ) -> JobState:
        """Patch the named properties of an existing job row (MERGE).

        The ``subscription_id`` / ``resource_group`` / ``cluster_name`` /
        ``storage_account`` parameters exist so a later poll can *backfill* the
        scope columns on a row that was first persisted without them (e.g. a
        ``/v1/jobs`` row synced before its cluster endpoint was resolvable). They
        are written verbatim when not None; the caller is responsible for only
        passing a value when it should overwrite an empty column. They are NOT
        re-derived from ``payload`` (the external payload nests its fields under
        an ``external`` key that ``canonical_job_metadata`` does not inspect).

        The ``job_title`` / ``program`` / ``db`` / ``query_label`` parameters
        exist for the same backfill reason: a ``/v1/jobs`` row first synced from
        a transient upstream row that lacked program/db was persisted with the
        canonical defaults (``program``/``job_title`` = ``"blast"``, ``db`` =
        ``""``). The list view reads these columns directly
        (``include_payload=False``), so a stuck degenerate value showed an API
        job as "blast" with no database. The sync backfill passes a value only
        when the stored column is the degenerate default and the upstream now
        carries a real value; like the scope args they are written verbatim and
        NOT re-derived from ``payload``.
        """
        with self._state_client() as t:
            try:
                e = dict(t.get_entity(partition_key=job_id, row_key="current"))
            except ResourceNotFoundError as exc:
                self._ensure_table("jobstate")
                raise KeyError(job_id) from exc
            # Submit only the changed properties (a MERGE *patch*) rather than
            # the whole read-back snapshot. ``UpdateMode.MERGE`` overwrites just
            # the properties present in the submitted entity, so writing back
            # the full snapshot reverted every field a concurrent writer had
            # changed since our read — e.g. the submit route's
            # ``update(job_id, task_id=...)`` racing the worker's
            # ``update(job_id, status="running")`` clobbered the fresh status
            # back to the stale "queued". The patch keeps PartitionKey/RowKey so
            # the SDK still targets the right row. Same-field writes remain
            # last-writer-wins (unchanged semantics); only cross-field
            # lost-updates are eliminated. ``e`` is still mutated in full so the
            # returned ``JobState`` reflects this call's changes.
            patch: dict[str, Any] = {"PartitionKey": job_id, "RowKey": "current"}
            if status is not None:
                e["status"] = status
                patch["status"] = status
            if phase is not None:
                e["phase"] = phase
                patch["phase"] = phase
            if task_id is not None:
                e["task_id"] = task_id
                patch["task_id"] = task_id
            if error_code is not None:
                e["error_code"] = error_code
                patch["error_code"] = error_code
            if payload is not None:
                import json

                payload_json = json.dumps(payload, default=str)
                e["payload_json"] = payload_json
                patch["payload_json"] = payload_json
                canonical = canonical_job_metadata(
                    payload,
                    job_id=job_id,
                    state_type=str(e.get("type") or ""),
                )
                e["schema_version"] = _JOB_SCHEMA_VERSION
                patch["schema_version"] = _JOB_SCHEMA_VERSION
                e.update(canonical)
                patch.update(canonical)
            # Explicit scope args are written AFTER the payload-canonical block
            # so a caller that passes both (payload + an explicit scope kwarg)
            # gets the explicit value, not the payload-derived one. Today the
            # only caller (`_sync_external_jobs_to_table` backfill) passes
            # scope-only, but ordering them last removes the silent-override
            # footgun for any future combined call.
            if subscription_id is not None:
                e["subscription_id"] = subscription_id
                patch["subscription_id"] = subscription_id
            if resource_group is not None:
                e["resource_group"] = resource_group
                patch["resource_group"] = resource_group
            if cluster_name is not None:
                e["cluster_name"] = cluster_name
                patch["cluster_name"] = cluster_name
            if storage_account is not None:
                e["storage_account"] = storage_account
                patch["storage_account"] = storage_account
            if job_title is not None:
                e["job_title"] = job_title
                patch["job_title"] = job_title
            if program is not None:
                e["program"] = program
                patch["program"] = program
            if db is not None:
                e["db"] = db
                patch["db"] = db
            if query_label is not None:
                e["query_label"] = query_label
                patch["query_label"] = query_label
            ts = updated_at or _now_iso()
            e["updated_at"] = ts
            patch["updated_at"] = ts
            t.update_entity(patch, mode=UpdateMode.MERGE)
        updated = JobState.from_entity(e)
        self.append_history(
            job_id,
            "update",
            {
                "status": status,
                "phase": phase,
                "error_code": error_code,
                "job_title": updated.job_title,
            },
        )
        # Secondary index sync: best-effort; owner_oid comes from the
        # read-back entity so no extra caller arg is needed.
        if updated.owner_oid:
            if status == "deleted":
                self._delete_idx_entry(updated.owner_oid, job_id, updated.created_at or "")
            else:
                self._write_idx_entry(updated.owner_oid, updated)
        return updated

    def _list_recent_sorted(
        self,
        filter_expr: str,
        *,
        limit: int,
        include_payload: bool,
    ) -> list[JobState]:
        """Return the genuinely most-recent ``limit`` rows matching ``filter_expr``.

        Azure Table Storage has no server-side ordering and jobstate rows use a
        random-uuid PartitionKey, so reading a single ``results_per_page=limit``
        page returns an arbitrary subset — sorting only that page silently drops
        the newest rows once the filter matches more than ``limit`` of them. This
        reads the full filtered set (bounded by :func:`_list_scan_hard_cap`, with
        ``$top`` clamped to the Azure page-size ceiling so the SDK paginates)
        before sorting by ``created_at`` descending and truncating to ``limit``.
        """
        scan_cap = _list_scan_hard_cap()
        rows: list[JobState] = []
        with self._state_client() as t:
            try:
                kwargs: dict[str, Any] = {"results_per_page": _clamp_page_size(scan_cap)}
                if not include_payload:
                    kwargs["select"] = _JOBSTATE_SUMMARY_SELECT
                entities = t.query_entities(filter_expr, **kwargs)
                for e in entities:
                    rows.append(JobState.from_entity(dict(e)))
                    if len(rows) >= scan_cap:
                        LOGGER.warning(
                            "jobstate list scan hit hard cap=%d (filter=%r); "
                            "most-recent ordering is best-effort beyond the cap — "
                            "consider a time-ordered secondary index",
                            scan_cap,
                            filter_expr,
                        )
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        rows.sort(key=lambda r: r.created_at or "", reverse=True)
        return rows[:limit]

    def list_for_owner(
        self,
        owner_oid: str,
        limit: int = 50,
        *,
        include_payload: bool = True,
    ) -> list[JobState]:
        """Return jobs owned by ``owner_oid`` plus cluster-shared rows.

        Rows with ``owner_oid=""`` are treated as cluster-shared: typically
        these come from the external OpenAPI sync and represent jobs that
        were originated by the BLAST runtime itself, not by a specific
        dashboard caller. Anyone with ARM scope on the cluster (which the
        caller-side filtering in the route layer enforces) can see them.

        The dashboard's own submit path always writes a concrete
        ``owner_oid`` so per-user privacy of submitted jobs is unchanged.

        Soft-deleted rows (``status='deleted'``) are filtered out: the
        delete route flips the row to that tombstone so a subsequent
        external sync skips re-creating it, but the user MUST NOT see
        the row in lists after they have asked to delete it.

        Ordering: the genuinely most-recent ``limit`` rows are returned even
        when the owner has more than ``limit`` jobs.  When the secondary
        ``jobstateidx`` table is available the method reads it (O(limit)
        rather than O(N)) and falls back to the full-scan path only on error.
        """
        try:
            rows, _cursor, _has_more = self.list_for_owner_indexed(
                owner_oid, limit, cursor=None
            )
            return rows
        except Exception as exc:
            LOGGER.warning(
                "jobstateidx read failed for owner=%r, falling back to full-scan: %s",
                owner_oid,
                exc,
            )
        safe_oid = _sanitise_odata_value(owner_oid)
        return self._list_recent_sorted(
            f"(owner_oid eq '{safe_oid}' or owner_oid eq '') and status ne 'deleted'",
            limit=limit,
            include_payload=include_payload,
        )

    def list_all(
        self,
        *,
        limit: int = 50,
        include_payload: bool = True,
    ) -> list[JobState]:
        """Return every non-deleted job row, owner-agnostic.

        Development-stage listing used by the unscoped ``/api/blast/jobs`` path
        when ``BLAST_JOBS_SHARED_VISIBILITY=true`` so the Recent searches page
        shows all jobs regardless of which caller submitted them. The route
        layer still enforces ``require_caller``. Production (flag off) keeps
        using :meth:`list_for_owner` so dashboard-submitted jobs stay private.

        Ordering follows :meth:`_list_recent_sorted`: the true most-recent
        ``limit`` rows, not an arbitrary first page.
        """
        return self._list_recent_sorted(
            "status ne 'deleted'",
            limit=limit,
            include_payload=include_payload,
        )

    def list_for_scope(
        self,
        *,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
        limit: int = 50,
        include_payload: bool = True,
    ) -> list[JobState]:
        """Return non-deleted jobs matching an explicit Azure/AKS scope.

        This is intentionally owner-agnostic and should only be used by route
        handlers that received an explicit scope from the caller (for example
        ``/api/blast/jobs?cluster_name=elb-cluster-01``). The Recent searches
        view reached from a cluster card is an operator surface: it should show
        the jobs running on that cluster even when the table row's ``owner_oid``
        differs from the currently signed-in browser user (common after
        switching Microsoft accounts, using a different tenant login, or
        syncing OpenAPI-originated jobs).

        The unscoped ``/api/blast/jobs`` path must keep using
        :meth:`list_for_owner` so personal dashboard-submitted jobs remain
        private by default.

        Scope semantics: ``cluster_name`` is the strongest key — an AKS
        cluster is the actual runtime that owns the job. When the caller
        provides ``cluster_name``, ``resource_group`` is intentionally NOT
        used as a hard filter. The dashboard's "workspace RG" (where
        Storage / ACR live) is a different concept from the cluster's RG
        (commonly ``rg-elb-cluster`` when the deploy helper's cluster-RG
        bootstrap pre-created it), so requiring both to match would hide
        jobs whose row was saved with the cluster RG. When ``cluster_name``
        is empty, ``resource_group`` falls back to a hard filter.
        """

        clauses = ["status ne 'deleted'"]
        if subscription_id:
            clauses.append(f"subscription_id eq '{_sanitise_odata_value(subscription_id)}'")
        if cluster_name:
            clauses.append(f"cluster_name eq '{_sanitise_odata_value(cluster_name)}'")
        elif resource_group:
            clauses.append(f"resource_group eq '{_sanitise_odata_value(resource_group)}'")
        if len(clauses) == 1:
            # Refuse an owner-agnostic global scan by accident.
            return []

        return self._list_recent_sorted(
            " and ".join(clauses),
            limit=limit,
            include_payload=include_payload,
        )

    def list_children(self, parent_job_id: str, limit: int = 100) -> list[JobState]:
        safe_parent = _sanitise_odata_value(parent_job_id)
        with self._state_client() as t:
            rows = []
            try:
                entities = t.query_entities(
                    f"parent_job_id eq '{safe_parent}'",
                    results_per_page=_clamp_page_size(limit),
                )
                for e in entities:
                    rows.append(JobState.from_entity(dict(e)))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        rows.sort(key=lambda r: r.created_at or "")
        return rows

    def list_active(
        self,
        *,
        job_type: str = "blast",
        limit: int = 500,
    ) -> list[JobState]:
        """Return jobs that are currently considered "in flight".

        Used by the reconciliation beat to find rows the worker is
        responsible for. Status values considered active:
        ``queued``, ``pending``, ``running``, ``reducing``.
        """
        active_states = ("queued", "pending", "running", "reducing")
        safe_type = _sanitise_odata_value(job_type)
        status_clause = " or ".join(
            f"status eq '{_sanitise_odata_value(s)}'" for s in active_states
        )
        filter_expr = f"type eq '{safe_type}' and ({status_clause})"
        rows: list[JobState] = []
        with self._state_client() as t:
            try:
                entities = t.query_entities(
                    filter_expr, results_per_page=_clamp_page_size(limit)
                )
                for e in entities:
                    rows.append(JobState.from_entity(dict(e)))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        return rows

    def list_completed(
        self,
        *,
        job_type: str = "blast",
        limit: int = 100,
    ) -> list[JobState]:
        """Return recently stored completed jobs for background backfill tasks."""
        safe_type = _sanitise_odata_value(job_type)
        filter_expr = f"type eq '{safe_type}' and status eq 'completed'"
        rows: list[JobState] = []
        with self._state_client() as t:
            try:
                entities = t.query_entities(
                    filter_expr, results_per_page=_clamp_page_size(limit)
                )
                for e in entities:
                    rows.append(JobState.from_entity(dict(e)))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        rows.sort(key=lambda r: r.updated_at or r.created_at or "", reverse=True)
        return rows

    def list_children_for_owner(
        self,
        owner_oid: str,
        parent_job_ids: list[str],
        *,
        limit: int = 5000,
    ) -> dict[str, list[JobState]]:
        """Return child job rows grouped by parent id using one Table query.

        Azure Tables does not give us a secondary index on ``parent_job_id``.
        The previous dashboard path queried once per parent, which made the
        Jobs card pay N sequential Table scans. This owner-scoped query keeps
        the same security boundary and filters the parent set in process.
        """
        parent_set = {parent for parent in parent_job_ids if parent}
        if not parent_set:
            return {}
        safe_oid = _sanitise_odata_value(owner_oid)
        grouped: dict[str, list[JobState]] = {parent: [] for parent in parent_set}
        with self._state_client() as t:
            try:
                entities = t.query_entities(
                    f"owner_oid eq '{safe_oid}' and parent_job_id ne ''",
                    results_per_page=_clamp_page_size(limit),
                )
                seen = 0
                for e in entities:
                    row = JobState.from_entity(dict(e))
                    if row.parent_job_id not in parent_set:
                        continue
                    grouped.setdefault(row.parent_job_id, []).append(row)
                    seen += 1
                    if seen >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        for rows in grouped.values():
            rows.sort(key=lambda r: r.created_at or "")
        return grouped

    # --- jobhistory ---

    def append_history(
        self, job_id: str, event: str, payload: dict[str, Any] | None = None
    ) -> None:
        try:
            import json

            entity = {
                "PartitionKey": job_id,
                "RowKey": _ulid_like(),
                "event": event,
                "ts": _now_iso(),
            }
            if payload is not None:
                # Audit P2 #13 #14: when STRICT_AUDIT_HASH=true, replace
                # GUID-shaped values under PII-bearing keys (caller_oid,
                # owner_oid, upn, …) with `redact_oid()` BEFORE the
                # payload is JSON-serialised and persisted. Default OFF
                # preserves the legacy verbose payload per charter §12a
                # Rule 4. The hash is deterministic so historical rows
                # remain joinable across events without recovering the
                # original GUID.
                effective_payload = (
                    _redact_audit_payload(payload)
                    if os.environ.get("STRICT_AUDIT_HASH", "").lower() == "true"
                    else payload
                )
                entity["payload_json"] = json.dumps(effective_payload, default=str)
            self._ensure_table("jobhistory")
            with self._history_client() as t:
                t.create_entity(entity)
        except Exception as exc:
            # History is best-effort — never fail the parent write because
            # the audit append failed.
            LOGGER.warning("append_history failed for %s: %s", job_id, exc)

    def get_history(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        safe_id = _sanitise_odata_value(job_id)
        with self._history_client() as t:
            rows = []
            try:
                entities = t.query_entities(
                    f"PartitionKey eq '{safe_id}'",
                    results_per_page=_clamp_page_size(limit),
                )
                for e in entities:
                    rows.append(dict(e))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobhistory")
        rows.sort(key=lambda r: r["RowKey"])
        return rows

    def get_history_for_jobs(
        self,
        job_ids: list[str],
        *,
        per_job_limit: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        """Bulk-fetch history for many jobs in a single Table query.

        The ``/api/audit/log`` route previously called ``get_history(job_id)``
        in a tight loop over the user's recent jobs (~20 calls per audit
        page render = 20 sequential Table round-trips). Azure Tables does
        not have a JOIN — but it does accept ``PartitionKey eq 'a' or
        PartitionKey eq 'b' or ...`` filter expressions, which the service
        evaluates as one query plan. We group locally afterwards and cap
        each partition at ``per_job_limit`` rows (matching the per-call
        contract of ``get_history``) so a single chatty job cannot crowd
        out the others in the audit pane.
        """
        if not job_ids:
            return {}
        unique_ids = list(dict.fromkeys(job_ids))
        clauses = " or ".join(
            f"PartitionKey eq '{_sanitise_odata_value(job_id)}'" for job_id in unique_ids
        )
        # Generous per-page so the SDK doesn't paginate mid-flight for a
        # 20-job batch with up to ``per_job_limit`` rows each. Clamp to
        # Azure Tables' hard max of 1000 entities per response.
        page_size = _clamp_page_size(
            max(per_job_limit, per_job_limit * len(unique_ids))
        )
        grouped: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in unique_ids}
        with self._history_client() as t:
            try:
                entities = t.query_entities(clauses, results_per_page=page_size)
                for e in entities:
                    row = dict(e)
                    partition = str(row.get("PartitionKey") or "")
                    bucket = grouped.get(partition)
                    if bucket is None or len(bucket) >= per_job_limit:
                        continue
                    bucket.append(row)
            except ResourceNotFoundError:
                self._ensure_table("jobhistory")
        for rows in grouped.values():
            rows.sort(key=lambda r: r.get("RowKey", ""))
        return grouped


# ---------------------------------------------------------------------------
# Module-level repository singleton
# ---------------------------------------------------------------------------
# Every request handler that touches job state currently does
# ``repo = JobStateRepository()`` which (a) re-resolves the managed-identity
# credential chain and (b) on first method call constructs a brand new
# ``TableClient`` (TLS handshake + pipeline setup) per route invocation.
# In the local dev profile the dashboard polls ``/api/blast/jobs`` every ~14s
# and each call enters several of these methods — multiplied by Korean→Azure
# RTT this dominates the endpoint's wall clock. The pooled-client wrapper
# above already keeps the HTTP pipeline alive across method calls on a single
# instance; this getter ensures hot routes actually reuse the same instance.

_DEFAULT_REPO: JobStateRepository | None = None
_DEFAULT_REPO_LOCK = threading.Lock()


def get_state_repo() -> JobStateRepository:
    """Return a process-wide :class:`JobStateRepository` singleton.

    Safe to call from any thread. Tests that rely on monkeypatching
    :data:`TableClient` or :data:`get_credential` should keep instantiating
    :class:`JobStateRepository` directly, or call :func:`reset_state_repo_cache`
    in a fixture to clear any cached instance.
    """

    global _DEFAULT_REPO
    repo = _DEFAULT_REPO
    if repo is not None:
        return repo
    with _DEFAULT_REPO_LOCK:
        if _DEFAULT_REPO is None:
            _DEFAULT_REPO = JobStateRepository()
        return _DEFAULT_REPO


def reset_state_repo_cache() -> None:
    """Drop the cached singleton repository. Intended for tests."""

    global _DEFAULT_REPO
    with _DEFAULT_REPO_LOCK:
        _DEFAULT_REPO = None
