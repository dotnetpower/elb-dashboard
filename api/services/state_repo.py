"""Azure Table-backed repositories for job state and history.

Responsibility: Azure Table-backed repositories for job state and history
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_payload_value`, `_basename`, `canonical_job_metadata`, `JobState`,
`JobStateRepository`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"  # eg https://stelb*.table.core.windows.net
_ENSURED_TABLES: set[tuple[str, str]] = set()
_JOB_SCHEMA_VERSION = 2
_JOBSTATE_SUMMARY_SELECT = [
    "PartitionKey",
    "RowKey",
    "schema_version",
    "type",
    "status",
    "phase",
    "owner_oid",
    "tenant_id",
    "parent_job_id",
    "task_id",
    "error_code",
    "created_at",
    "updated_at",
    "job_title",
    "program",
    "db",
    "query_label",
    "subscription_id",
    "resource_group",
    "cluster_name",
    "storage_account",
]


def _payload_value(payload: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _basename(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "az://")):
        parsed = urlparse(
            "https://" + raw.removeprefix("az://") if raw.startswith("az://") else raw
        )
        parts = [part for part in parsed.path.replace("\\", "/").split("/") if part]
        if parts:
            return parts[-1]
    parts = [part for part in raw.replace("\\", "/").split("/") if part]
    return parts[-1] if parts else raw


def canonical_job_metadata(
    payload: dict[str, Any] | None,
    *,
    job_id: str,
    state_type: str = "blast",
) -> dict[str, str]:
    """Return the canonical v2 job columns derived from a submit payload."""
    program = str(_payload_value(payload, "program") or "blast").strip() or "blast"
    db = _basename(_payload_value(payload, "db", "database"))
    query_label = _basename(
        _payload_value(payload, "query_label", "query_file", "query_name", "query_blob_url")
    )
    explicit_title = str(_payload_value(payload, "job_title", "title") or "").strip()
    if explicit_title:
        job_title = explicit_title
    elif state_type == "warmup" and db:
        job_title = f"Warm up - {db}"
    else:
        title_parts = [part for part in (program, db, query_label) if part]
        job_title = " - ".join(title_parts) if title_parts else job_id
    return {
        "job_title": job_title[:240],
        "program": program[:64],
        "db": db[:240],
        "query_label": query_label[:240],
        "subscription_id": str(_payload_value(payload, "subscription_id") or "")[:64],
        "resource_group": str(_payload_value(payload, "resource_group") or "")[:120],
        "cluster_name": str(_payload_value(payload, "cluster_name", "aks_cluster_name") or "")[
            :120
        ],
        "storage_account": str(_payload_value(payload, "storage_account") or "")[:64],
    }


@dataclass
class JobState:
    job_id: str
    type: str
    status: str  # queued|running|completed|failed|cancelled
    phase: str | None = None
    owner_oid: str | None = None
    tenant_id: str | None = None
    parent_job_id: str | None = None
    task_id: str | None = None
    error_code: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    payload: dict[str, Any] | None = None
    schema_version: int = _JOB_SCHEMA_VERSION
    job_title: str | None = None
    program: str | None = None
    db: str | None = None
    query_label: str | None = None
    subscription_id: str | None = None
    resource_group: str | None = None
    cluster_name: str | None = None
    storage_account: str | None = None

    def to_entity(self) -> dict[str, Any]:
        canonical = canonical_job_metadata(
            self.payload,
            job_id=self.job_id,
            state_type=self.type,
        )
        e: dict[str, Any] = {
            "PartitionKey": self.job_id,
            "RowKey": "current",
            "schema_version": self.schema_version or _JOB_SCHEMA_VERSION,
            "type": self.type,
            "status": self.status,
            "phase": self.phase or "",
            "owner_oid": self.owner_oid or "",
            "tenant_id": self.tenant_id or "",
            "parent_job_id": self.parent_job_id or "",
            "task_id": self.task_id or "",
            "error_code": self.error_code or "",
            "created_at": self.created_at or "",
            "updated_at": self.updated_at or "",
            "job_title": self.job_title or canonical["job_title"],
            "program": self.program or canonical["program"],
            "db": self.db or canonical["db"],
            "query_label": self.query_label or canonical["query_label"],
            "subscription_id": self.subscription_id or canonical["subscription_id"],
            "resource_group": self.resource_group or canonical["resource_group"],
            "cluster_name": self.cluster_name or canonical["cluster_name"],
            "storage_account": self.storage_account or canonical["storage_account"],
        }
        if self.payload is not None:
            import json

            e["payload_json"] = json.dumps(self.payload, default=str)
        return e

    @classmethod
    def from_entity(cls, e: dict[str, Any]) -> JobState:
        import json

        payload = None
        if e.get("payload_json"):
            try:
                payload = json.loads(e["payload_json"])
            except Exception:
                payload = None
        canonical = canonical_job_metadata(
            payload,
            job_id=e["PartitionKey"],
            state_type=e.get("type", ""),
        )
        return cls(
            job_id=e["PartitionKey"],
            type=e.get("type", ""),
            status=e.get("status", ""),
            phase=e.get("phase") or None,
            owner_oid=e.get("owner_oid") or None,
            tenant_id=e.get("tenant_id") or None,
            parent_job_id=e.get("parent_job_id") or None,
            task_id=e.get("task_id") or None,
            error_code=e.get("error_code") or None,
            created_at=e.get("created_at") or None,
            updated_at=e.get("updated_at") or None,
            payload=payload,
            schema_version=int(e.get("schema_version") or _JOB_SCHEMA_VERSION),
            job_title=e.get("job_title") or canonical["job_title"],
            program=e.get("program") or canonical["program"],
            db=e.get("db") or canonical["db"],
            query_label=e.get("query_label") or canonical["query_label"],
            subscription_id=e.get("subscription_id") or canonical["subscription_id"],
            resource_group=e.get("resource_group") or canonical["resource_group"],
            cluster_name=e.get("cluster_name") or canonical["cluster_name"],
            storage_account=e.get("storage_account") or canonical["storage_account"],
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ulid_like() -> str:
    """Sortable id (timestamp-prefixed) suitable for jobhistory RowKey."""
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def _sanitise_odata_value(v: str) -> str:
    """Escape a value for safe interpolation into OData filter expressions.

    OData single-quoted strings require escaping the single quote by doubling it.
    Additionally reject any control characters.
    """
    import re

    if re.search(r"[\x00-\x1f]", v):
        raise ValueError("control characters not allowed in OData value")
    return v.replace("'", "''")


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

    def _state_client(self) -> TableClient:
        return TableClient(endpoint=self._endpoint, table_name="jobstate", credential=self._cred)

    def _history_client(self) -> TableClient:
        return TableClient(endpoint=self._endpoint, table_name="jobhistory", credential=self._cred)

    def _ensure_table(self, table_name: str) -> None:
        key = (self._endpoint, table_name)
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
            e["updated_at"] = _now_iso()
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
                    f"parent_job_id eq '{safe_parent}'", results_per_page=limit
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
                entities = t.query_entities(filter_expr, results_per_page=limit)
                for e in entities:
                    rows.append(JobState.from_entity(dict(e)))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobstate")
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
                    results_per_page=limit,
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
                entities = t.query_entities(f"PartitionKey eq '{safe_id}'", results_per_page=limit)
                for e in entities:
                    rows.append(dict(e))
                    if len(rows) >= limit:
                        break
            except ResourceNotFoundError:
                self._ensure_table("jobhistory")
        rows.sort(key=lambda r: r["RowKey"])
        return rows
