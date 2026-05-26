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
                return existing
            raise
        created = JobState.from_entity(entity)
        self.append_history(
            created.job_id,
            "created",
            {"status": created.status, "phase": created.phase, "job_title": created.job_title},
        )
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
    ) -> JobState:
        with self._state_client() as t:
            try:
                e = dict(t.get_entity(partition_key=job_id, row_key="current"))
            except ResourceNotFoundError as exc:
                self._ensure_table("jobstate")
                raise KeyError(job_id) from exc
            if status is not None:
                e["status"] = status
            if phase is not None:
                e["phase"] = phase
            if task_id is not None:
                e["task_id"] = task_id
            if error_code is not None:
                e["error_code"] = error_code
            if payload is not None:
                import json

                e["payload_json"] = json.dumps(payload, default=str)
                canonical = canonical_job_metadata(
                    payload,
                    job_id=job_id,
                    state_type=str(e.get("type") or ""),
                )
                e["schema_version"] = _JOB_SCHEMA_VERSION
                e.update(canonical)
            e["updated_at"] = updated_at or _now_iso()
            t.update_entity(e, mode=UpdateMode.MERGE)
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
        return updated

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
        """
        safe_oid = _sanitise_odata_value(owner_oid)
        with self._state_client() as t:
            rows = []
            try:
                kwargs: dict[str, Any] = {"results_per_page": limit}
                if not include_payload:
                    kwargs["select"] = _JOBSTATE_SUMMARY_SELECT
                entities = t.query_entities(
                    f"(owner_oid eq '{safe_oid}' or owner_oid eq '') "
                    "and status ne 'deleted'",
                    **kwargs,
                )
                for e in entities:
                    rows.append(JobState.from_entity(dict(e)))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
        rows.sort(key=lambda r: r.created_at or "", reverse=True)
        return rows

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
                entity["payload_json"] = json.dumps(payload, default=str)
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
