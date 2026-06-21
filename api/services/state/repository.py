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
The optional time-ordered index (#50) is flag-gated by `time_index_enabled()`
and writes an IMMUTABLE index row keyed on `owner_oid` + `created_at`; never
key it on a mutable field.
Validation: `uv run pytest -q api/tests/test_state_repo.py api/tests/test_jobstate_time_index.py`.
"""

from __future__ import annotations

import logging
import os
import threading
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
from api.services.state.time_index import (
    ALL_BUCKET,
    INDEX_TABLE_NAME,
    decode_cursor,
    encode_cursor,
    index_buckets,
    index_entities,
    owner_bucket,
    row_key,
    time_index_enabled,
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


# Max number of job_ids folded into a single `get_many` OData `$filter`.
# Each id contributes a `(PartitionKey eq '<id>' and RowKey eq 'current')`
# clause (~70 bytes) joined by ` or `. Azure Table Storage carries the
# filter in the request URI, so an unbounded clause count produces an
# over-length request that fails with HTTP 400 — the error is swallowed by
# callers that wrap `get_many` in a best-effort try/except, which silently
# degrades a batch lookup to "nothing found". The external-jobs sync hit
# this with 1029 ids: every poll saw an empty existing-map, re-`create()`d
# all 1029 rows (each a 409 + point-read round-trip), and pinned the api
# sidecar at 100% CPU. Chunking keeps every filter small and the lookup
# correct regardless of batch size. 50 clauses ≈ 3.7 KB — far under any
# practical URI limit.
_GET_MANY_FILTER_CHUNK = 50


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
        self._index_pool: _PooledTableClient | None = None
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

    def _index_client(self) -> _PooledTableClient:
        """Pooled client for the optional time-ordered index table (#50).

        Lazily constructed like the other clients; only ever touched when
        ``time_index_enabled()`` is true, so a deployment with the feature off
        never opens a pipeline against ``jobstateindex``.
        """
        pool = self._index_pool
        if pool is None:
            with self._pool_lock:
                pool = self._index_pool
                if pool is None:
                    pool = _PooledTableClient(
                        TableClient(
                            endpoint=self._endpoint,
                            table_name=INDEX_TABLE_NAME,
                            credential=self._cred,
                        )
                    )
                    self._index_pool = pool
        return pool

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

    # --- time-ordered index (#50, flag-gated) ---

    def _index_put(self, state: JobState) -> None:
        """Best-effort add of a job's immutable index row (on create).

        Never raises: the ``jobstate`` row is the source of truth, so an index
        write failure must not fail the create. It is logged + counted so a
        sustained failure is visible, and the backfill script reconciles any
        rows the index missed. Only called when ``time_index_enabled()``.
        """
        entities = index_entities(
            job_id=state.job_id,
            owner_oid=state.owner_oid,
            created_at=state.created_at,
        )
        try:
            self._ensure_table(INDEX_TABLE_NAME)
            with self._index_client() as t:
                for entity in entities:
                    t.upsert_entity(entity)
        except Exception as exc:
            LOGGER.warning(
                "jobstate time-index put failed job_id=%s: %s",
                state.job_id,
                type(exc).__name__,
            )

    def _index_delete(self, *, job_id: str, owner_oid: str | None, created_at: str | None) -> None:
        """Best-effort removal of a job's index row (on soft-delete).

        Idempotent: deleting an already-absent row is a no-op (the SDK raises
        ``ResourceNotFoundError`` which we swallow). Never raises. Only called
        when ``time_index_enabled()``.
        """
        rk = row_key(created_at, job_id)
        try:
            with self._index_client() as t:
                for partition in index_buckets(owner_oid):
                    try:
                        t.delete_entity(partition_key=partition, row_key=rk)
                    except ResourceNotFoundError:
                        continue
        except Exception as exc:
            LOGGER.warning(
                "jobstate time-index delete failed job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )

    def reconcile_time_index(
        self, *, dry_run: bool = False, batch_log_every: int = 500
    ) -> tuple[int, int]:
        """Idempotently (re)build the time-ordered index from ``jobstate`` rows.

        Returns ``(scanned, written)``. Read-only against ``jobstate``,
        upsert-only against ``jobstateindex`` — never deletes or mutates a
        ``jobstate`` row. Streams the source table so memory stays bounded
        regardless of history size.

        Shared by the one-shot backfill script
        (``scripts/dev/backfill_jobstate_time_index.py``) and the periodic
        reconcile task (``api.tasks.blast.reconcile_time_index``). Both rely on
        the same property: the index RowKey is derived only from the immutable
        ``owner_oid`` + ``created_at``, so re-running upserts the SAME RowKey per
        job — a partial run is safely resumable and a steady-state reconcile is a
        no-op write-for-write.

        Heals the only gap the best-effort write path can open: an ``_index_put``
        that failed after the ``jobstate`` row was written silently OMITS that
        job from the indexed listing until a reconcile re-adds it. The mirror
        case — an ``_index_delete`` that failed, leaving a stale index row for a
        soft-deleted job — needs no cleanup here because ``list_owner_page``
        already skips ``status='deleted'`` (and missing) rows at read time; this
        pass simply does not re-add tombstones (the ``status ne 'deleted'``
        filter below).

        ``dry_run`` counts what WOULD be written and touches no index table (it
        does not even create ``jobstateindex``), so it is safe to run before a
        flip to size the backfill.
        """
        written = 0
        scanned = 0

        if not dry_run:
            self._ensure_table(INDEX_TABLE_NAME)

        # Read only the columns needed to build the index key; skip the large
        # payload. ``status ne 'deleted'`` mirrors the listing filter so
        # tombstones are not indexed.
        select = ["PartitionKey", "RowKey", "owner_oid", "created_at", "status"]
        with self._state_client() as state_t:
            try:
                entities = state_t.query_entities(
                    "RowKey eq 'current' and status ne 'deleted'",
                    results_per_page=1000,
                    select=select,
                )
            except ResourceNotFoundError:
                # jobstate table not created yet -> nothing to reconcile.
                return 0, 0

            # The index client is POOLED and owned by this repository — do NOT
            # close it here. Closing the shared pooled client would tear down the
            # underlying HTTP transport, so the next caller in the same process
            # (the periodic reconcile task re-runs this every tick) would then
            # operate on a closed client and fail. The pool is reclaimed on
            # process exit / ``reset_state_repo_cache()``.
            index_t = None if dry_run else self._index_client()
            for entity in entities:
                scanned += 1
                job_id = str(entity.get("PartitionKey") or "")
                if not job_id:
                    continue
                for index_entity in index_entities(
                    job_id=job_id,
                    owner_oid=entity.get("owner_oid"),
                    created_at=entity.get("created_at"),
                ):
                    if not dry_run and index_t is not None:
                        index_t.upsert_entity(index_entity)
                written += 1
                if batch_log_every and written % batch_log_every == 0:
                    LOGGER.info(
                        "jobstate time-index reconcile progress scanned=%d written=%d",
                        scanned,
                        written,
                    )

        return scanned, written

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
        if time_index_enabled():
            self._index_put(created)
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

    def get_many(
        self, job_ids: list[str], *, select: list[str] | None = None
    ) -> dict[str, JobState]:
        """Batch lookup for N job_ids using a single OData query.

        ``select`` optionally projects a subset of columns (e.g.
        ``_JOBSTATE_SUMMARY_SELECT`` to skip the large ``payload_json``).

        Returns a dict mapping job_id -> JobState for rows that exist.
        Missing job_ids are simply absent from the result.

        The lookup is chunked into batches of ``_GET_MANY_FILTER_CHUNK`` ids so
        the OData ``$filter`` stays within a safe request-URI length regardless
        of how many ids the caller passes (the external-jobs sync can pass 1000+).
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
        result: dict[str, JobState] = {}
        # Chunk the lookup so the OData `$filter` never grows past a safe URI
        # length. A single mega-filter (e.g. 1029 ids from the external-jobs
        # sync) fails with HTTP 400 and, because best-effort callers swallow the
        # error, degrades to "nothing found" — which makes the sync re-create
        # every row on every poll. See `_GET_MANY_FILTER_CHUNK`.
        with self._state_client() as t:
            for start in range(0, len(unique_ids), _GET_MANY_FILTER_CHUNK):
                chunk = unique_ids[start : start + _GET_MANY_FILTER_CHUNK]
                parts = [
                    f"(PartitionKey eq '{_sanitise_odata_value(jid)}' and RowKey eq 'current')"
                    for jid in chunk
                ]
                filter_expr = " or ".join(parts)
                query_kwargs: dict[str, Any] = {
                    "results_per_page": _clamp_page_size(len(chunk))
                }
                if select is not None:
                    query_kwargs["select"] = select
                try:
                    for e in t.query_entities(filter_expr, **query_kwargs):
                        state = JobState.from_entity(dict(e))
                        result[state.job_id] = state
                except ResourceNotFoundError:
                    self._ensure_table("jobstate")
                    break
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
        result_manifest: str | None = None,
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
            if result_manifest is not None:
                e["result_manifest"] = result_manifest
                patch["result_manifest"] = result_manifest
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
        # Soft-delete (status flipped to the 'deleted' tombstone) removes the
        # job from every listing, so drop its index row too (#50). owner_oid /
        # created_at are immutable, so the RowKey here matches the one written
        # at create. Other status transitions never touch the index.
        if time_index_enabled() and status == "deleted":
            self._index_delete(
                job_id=job_id,
                owner_oid=updated.owner_oid,
                created_at=updated.created_at,
            )
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
        when the owner has more than ``limit`` jobs — see
        :meth:`_list_recent_sorted` for why a page-sized read is not enough.

        When ``time_index_enabled()`` the bounded time-ordered index (#50) is
        used instead of the full scan; an index miss / error falls back to the
        legacy scan below so the listing is never empty due to an
        un-backfilled or unavailable index.
        """
        if time_index_enabled():
            try:
                rows, _cursor = self.list_owner_page(
                    owner_oid, limit=limit, include_payload=include_payload
                )
            except Exception as exc:
                LOGGER.warning(
                    "jobstate time-index read failed (owner); using legacy scan: %s",
                    type(exc).__name__,
                )
                rows = []
            if rows:
                return rows
            # Empty index result: could be a genuinely empty owner OR an
            # un-backfilled index. Fall back to the legacy scan so jobs are
            # never hidden; the scan returns [] too when the owner truly has
            # none, so this only costs one extra read in the empty case.
            LOGGER.info(
                "jobstate time-index returned no rows (owner); falling back to legacy scan"
            )
        safe_oid = _sanitise_odata_value(owner_oid)
        return self._list_recent_sorted(
            f"(owner_oid eq '{safe_oid}' or owner_oid eq '') and status ne 'deleted'",
            limit=limit,
            include_payload=include_payload,
        )

    def list_owner_page(
        self,
        owner_oid: str,
        *,
        limit: int = 50,
        include_payload: bool = True,
        cursor: str = "",
    ) -> tuple[list[JobState], str | None]:
        """Indexed most-recent page for an owner (#50): ``(rows, next_cursor)``.

        Reads the time-ordered index instead of scanning ``jobstate``. The
        owner's filter (``owner_oid eq X or owner_oid eq ''``) maps to exactly
        two index partitions — the owner's own bucket and the shared bucket —
        each of which is read newest-first for ``limit + 1`` rows (the extra row
        is the honest ``has_more`` probe). The two streams are merged by RowKey
        (inverted ticks => newest first), truncated to ``limit``, and the job
        rows are batch-fetched from ``jobstate`` in that order.

        ``cursor`` is an opaque token from a previous page's ``next_cursor``;
        an invalid/expired cursor degrades to the first page. ``next_cursor`` is
        ``None`` when no further rows exist.

        Raises on a hard index/Table error so ``list_for_owner`` can fall back
        to the legacy scan. Rows whose ``jobstate`` entry is missing or already
        tombstoned (``status='deleted'``) are skipped defensively (a delete that
        raced the index read).
        """
        page_size = _clamp_page_size(limit + 1)
        after = decode_cursor(cursor)
        buckets: list[str] = []
        for bucket in (owner_bucket(owner_oid), owner_bucket("")):
            if bucket not in buckets:
                buckets.append(bucket)

        # (row_key, job_id) newest-first across both buckets.
        merged: list[tuple[str, str]] = []
        with self._index_client() as t:
            for bucket in buckets:
                clauses = [f"PartitionKey eq '{_sanitise_odata_value(bucket)}'"]
                if after:
                    clauses.append(f"RowKey gt '{_sanitise_odata_value(after)}'")
                filter_expr = " and ".join(clauses)
                taken = 0
                try:
                    for entity in t.query_entities(
                        filter_expr,
                        results_per_page=page_size,
                        select=["RowKey", "job_id"],
                    ):
                        rk = str(entity.get("RowKey") or "")
                        jid = str(entity.get("job_id") or "")
                        if not rk or not jid:
                            continue
                        merged.append((rk, jid))
                        taken += 1
                        if taken >= page_size:
                            break
                except ResourceNotFoundError:
                    # Index table not created yet -> treat as empty (caller
                    # falls back to the legacy scan).
                    self._ensure_table(INDEX_TABLE_NAME)

        if not merged:
            return [], None

        merged.sort(key=lambda pair: pair[0])
        has_more = len(merged) > limit
        window = merged[:limit]
        ordered_job_ids = [jid for _rk, jid in window]

        select = None if include_payload else _JOBSTATE_SUMMARY_SELECT
        states = self.get_many(ordered_job_ids, select=select)

        rows: list[JobState] = []
        for _rk, jid in window:
            state = states.get(jid)
            if state is None or state.status == "deleted":
                continue
            rows.append(state)

        next_cursor = encode_cursor(window[-1][0]) if (has_more and window) else None
        return rows, next_cursor


    def list_all_page(
        self,
        *,
        limit: int = 50,
        include_payload: bool = True,
        cursor: str = "",
    ) -> tuple[list[JobState], str | None]:
        """Indexed most-recent page across ALL owners (#50): ``(rows, next_cursor)``.

        Reads the single global :data:`ALL_BUCKET` index partition newest-first
        for ``limit + 1`` rows (the extra row is the honest ``has_more`` probe)
        instead of scanning ``jobstate``. Within one partition Azure returns
        rows in RowKey order, so no cross-bucket merge is needed (unlike
        :meth:`list_owner_page`). ``cursor`` is an opaque token from a previous
        page's ``next_cursor``; an invalid/expired cursor degrades to the first
        page.

        Raises on a hard index/Table error so :meth:`list_all` can fall back to
        the legacy scan. Rows whose ``jobstate`` entry is missing or tombstoned
        (``status='deleted'``) are skipped defensively (a delete that raced the
        index read).
        """
        page_size = _clamp_page_size(limit + 1)
        after = decode_cursor(cursor)
        clauses = [f"PartitionKey eq '{_sanitise_odata_value(ALL_BUCKET)}'"]
        if after:
            clauses.append(f"RowKey gt '{_sanitise_odata_value(after)}'")
        filter_expr = " and ".join(clauses)

        index_rows: list[tuple[str, str]] = []
        with self._index_client() as t:
            try:
                taken = 0
                for entity in t.query_entities(
                    filter_expr,
                    results_per_page=page_size,
                    select=["RowKey", "job_id"],
                ):
                    rk = str(entity.get("RowKey") or "")
                    jid = str(entity.get("job_id") or "")
                    if not rk or not jid:
                        continue
                    index_rows.append((rk, jid))
                    taken += 1
                    if taken >= page_size:
                        break
            except ResourceNotFoundError:
                # Index table not created yet -> empty (caller falls back).
                self._ensure_table(INDEX_TABLE_NAME)

        if not index_rows:
            return [], None

        # Single partition is already RowKey-ordered, but sort defensively so the
        # contract holds even if the SDK ever returns unordered pages.
        index_rows.sort(key=lambda pair: pair[0])
        has_more = len(index_rows) > limit
        window = index_rows[:limit]
        ordered_job_ids = [jid for _rk, jid in window]

        select = None if include_payload else _JOBSTATE_SUMMARY_SELECT
        states = self.get_many(ordered_job_ids, select=select)

        rows: list[JobState] = []
        for _rk, jid in window:
            state = states.get(jid)
            if state is None or state.status == "deleted":
                continue
            rows.append(state)

        next_cursor = encode_cursor(window[-1][0]) if (has_more and window) else None
        return rows, next_cursor

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

        Ordering: the genuinely most-recent ``limit`` rows, not an arbitrary
        first page. When ``time_index_enabled()`` the bounded global
        :data:`ALL_BUCKET` index (#50) is used; an index miss / error falls back
        to the legacy scan so the listing is never empty due to an un-backfilled
        or unavailable index (mirrors :meth:`list_for_owner`).
        """
        if time_index_enabled():
            try:
                rows, _cursor = self.list_all_page(
                    limit=limit, include_payload=include_payload
                )
            except Exception as exc:
                LOGGER.warning(
                    "jobstate time-index read failed (all); using legacy scan: %s",
                    type(exc).__name__,
                )
                rows = []
            if rows:
                return rows
            LOGGER.info(
                "jobstate time-index returned no rows (all); falling back to legacy scan"
            )
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

        # #50 note: this path intentionally stays on the bounded legacy scan and
        # is NOT served by the time-ordered index. The index key is immutable
        # (owner_oid + created_at), but ``cluster_name`` / ``subscription_id`` /
        # ``resource_group`` are MUTABLE — ``update()`` backfills them after
        # create — so a scope-keyed index row would have to MOVE when the scope
        # is filled in, breaking the add-on-create / remove-on-delete invariant
        # the index relies on. ``list_for_scope`` is also an operator surface
        # (explicit cluster scope from a route), not the hot per-owner default
        # path, so the scan is acceptable here.
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
        """Return the genuinely most-recently-completed jobs for backfill tasks.

        Ordering is load-bearing here. jobstate rows use a random-uuid
        PartitionKey, so a single capped page (``results_per_page=limit``)
        returns an arbitrary, FIXED lexical subset. The backfill task skips
        rows that already carry runtime metrics, so once that fixed window is
        fully backfilled every later tick re-scans the same rows and makes zero
        progress — any completed job outside the window is silently starved
        forever (same bug class as the auto-stop ``history_scan_truncated``
        regression).

        This reads the full filtered set as lightweight summaries (bounded by
        the same hard cap as :meth:`_list_recent_sorted`), sorts by
        ``updated_at`` (completion recency) descending, then re-fetches the full
        payload only for the top ``limit`` rows the caller will actually
        process. ``updated_at`` — not ``created_at`` — is the correct key: a
        long-running BLAST job can be created hours before it completes, and
        only recently-completed jobs still have a live K8s Job to read container
        timestamps from (old ones are garbage-collected, so backfilling them is
        a no-op). The caller needs ``payload`` (scope + existing metrics), so
        the summaries are not returned directly.
        """
        safe_type = _sanitise_odata_value(job_type)
        filter_expr = f"type eq '{safe_type}' and status eq 'completed'"
        scan_cap = _list_scan_hard_cap()
        summaries: list[JobState] = []
        with self._state_client() as t:
            try:
                entities = t.query_entities(
                    filter_expr,
                    results_per_page=_clamp_page_size(scan_cap),
                    select=_JOBSTATE_SUMMARY_SELECT,
                )
                for e in entities:
                    summaries.append(JobState.from_entity(dict(e)))
                    if len(summaries) >= scan_cap:
                        LOGGER.warning(
                            "list_completed scan hit hard cap=%d (type=%r); "
                            "backfill ordering is best-effort beyond the cap",
                            scan_cap,
                            job_type,
                        )
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        summaries.sort(key=lambda r: r.updated_at or r.created_at or "", reverse=True)
        rows: list[JobState] = []
        for summary in summaries[: max(0, limit)]:
            full = self.get(summary.job_id)
            if full is not None:
                rows.append(full)
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
        grouped: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in unique_ids}
        # Chunk the PartitionKey-OR filter the same way ``get_many`` does so a
        # large ``job_ids`` batch never builds an over-length OData filter that
        # fails with HTTP 400 (the get_many CPU-storm bug). The audit route caps
        # the input at 20 today, but the function contract accepts any list.
        with self._history_client() as t:
            for start in range(0, len(unique_ids), _GET_MANY_FILTER_CHUNK):
                chunk = unique_ids[start : start + _GET_MANY_FILTER_CHUNK]
                clauses = " or ".join(
                    f"PartitionKey eq '{_sanitise_odata_value(job_id)}'" for job_id in chunk
                )
                # Generous per-page so the SDK doesn't paginate mid-flight for a
                # chunk with up to ``per_job_limit`` rows each. Clamp to Azure
                # Tables' hard max of 1000 entities per response.
                page_size = _clamp_page_size(max(per_job_limit, per_job_limit * len(chunk)))
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
                    break
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
