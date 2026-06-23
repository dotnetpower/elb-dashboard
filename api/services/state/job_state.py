"""`JobState` dataclass + canonical metadata helpers.

Responsibility: Hold the `JobState` dataclass, the canonical metadata
derivation (`canonical_job_metadata`), and the small parsing/formatting
helpers shared by it and the repository.
Edit boundaries: Pure data shaping. No Azure SDK calls, no I/O.
Key entry points: `JobState`, `canonical_job_metadata`, `_payload_value`,
`_basename`, `_now_iso`, `_ulid_like`, `_sanitise_odata_value`,
`_JOB_SCHEMA_VERSION`, `_JOBSTATE_SUMMARY_SELECT`.
Risky contracts: `_sanitise_odata_value` rejects control characters and doubles
single quotes — every OData filter built by the repository goes through it.
Validation: `uv run pytest -q api/tests/test_state_repo.py`.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

_JOB_SCHEMA_VERSION = 2
_JOBSTATE_SUMMARY_SELECT = [
    "PartitionKey",
    "RowKey",
    "schema_version",
    "type",
    "status",
    "phase",
    "owner_oid",
    "owner_upn",
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
    "external_correlation_id",
    "submission_source",
]


def _payload_value(payload: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _resolve_external_correlation_id(payload: dict[str, Any] | None) -> str:
    """Extract the Service Bus / external correlation id from a job payload.

    Prefers the nested ``payload.external.external_correlation_id`` (queue-drained
    rows stamp it there) then the payload top level (the send-time placeholder).
    Returns ``""`` when absent so the caller can fall back to a stored column.
    """
    if not isinstance(payload, dict):
        return ""
    external = payload.get("external")
    if isinstance(external, dict):
        nested = str(external.get("external_correlation_id") or "").strip()
        if nested:
            return nested
    return str(payload.get("external_correlation_id") or "").strip()


def _resolve_payload_submission_source(payload: dict[str, Any] | None) -> str:
    """Extract submission_source from a job payload (nested external first)."""
    if not isinstance(payload, dict):
        return ""
    external = payload.get("external")
    if isinstance(external, dict):
        nested = str(external.get("submission_source") or "").strip()
        if nested:
            return nested
    return str(payload.get("submission_source") or "").strip()


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
    owner_upn: str | None = None
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
    # The originating Service Bus / external request correlation id and the
    # server-derived submission_source are persisted as durable columns (not
    # only inside ``payload``) so the Recent searches / Jobs list -- which reads
    # columns with ``include_payload=False`` -- can show the true queue origin
    # and let an operator trace a request from the Service Bus queue to its job.
    external_correlation_id: str | None = None
    submission_source: str | None = None
    # JSON array of ``{file_id, blob_path}`` for an external job's result files,
    # captured at the succeeded transition (cluster up) so the download route
    # can stream the result straight from Storage when the elb-openapi proxy is
    # unreachable (cluster auto-stopped). Blob paths are relative to
    # ``results/{job_id}/`` (the sibling's contract).
    result_manifest: str | None = None
    # Canonical results-container prefix for this job (system-of-record). When
    # unset, ``to_entity`` defaults it to ``{job_id}/`` (the legacy flat layout)
    # so every row carries it; issue #67 writes a date-tiered value here for
    # dashboard-submitted jobs while external (``/v1/jobs``) jobs keep the flat
    # default per the sibling's contract. Readers resolve it through
    # ``api.services.storage.job_prefix`` rather than reconstructing it.
    results_prefix: str | None = None

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
            "owner_upn": self.owner_upn or "",
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
            # Resolve from the explicit field first, then from the payload
            # (queue-drained rows stamp these only inside ``payload.external``),
            # so the durable column is backfilled even when the caller built the
            # row with just a payload.
            "external_correlation_id": self.external_correlation_id
            or _resolve_external_correlation_id(self.payload),
            "submission_source": self.submission_source
            or _resolve_payload_submission_source(self.payload),
            "result_manifest": self.result_manifest or "",
            # Persist the canonical results prefix on every row so readers can
            # resolve it without reconstructing ``{job_id}/``. Defaults to the
            # legacy flat layout when not explicitly set (issue #67 overrides it
            # for dashboard-submitted jobs).
            "results_prefix": self.results_prefix or f"{self.job_id}/",
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
            owner_upn=e.get("owner_upn") or None,
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
            external_correlation_id=e.get("external_correlation_id") or None,
            submission_source=e.get("submission_source") or None,
            result_manifest=e.get("result_manifest") or None,
            results_prefix=e.get("results_prefix") or None,
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ulid_like() -> str:
    """Sortable id (timestamp-prefixed) suitable for jobhistory RowKey."""
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")


def _sanitise_odata_value(v: str) -> str:
    """Escape a value for safe interpolation into OData filter expressions.

    OData single-quoted strings require escaping the single quote by doubling it.
    Additionally reject any control characters.
    """
    if _CONTROL_CHAR_RE.search(v):
        raise ValueError("control characters not allowed in OData value")
    return v.replace("'", "''")
