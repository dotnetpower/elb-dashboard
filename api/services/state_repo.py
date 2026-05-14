"""Storage table-backed repositories for job state and history.

`jobstate` table — one row per job, PartitionKey=job_id, RowKey="current".
`jobhistory` table — many rows per job, PartitionKey=job_id, RowKey=ulid.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"  # eg https://stelb*.table.core.windows.net


@dataclass
class JobState:
    job_id: str
    type: str
    status: str  # queued|running|completed|failed|cancelled
    phase: str | None = None
    owner_oid: str | None = None
    tenant_id: str | None = None
    task_id: str | None = None
    error_code: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    payload: dict[str, Any] | None = None

    def to_entity(self) -> dict[str, Any]:
        e: dict[str, Any] = {
            "PartitionKey": self.job_id,
            "RowKey": "current",
            "type": self.type,
            "status": self.status,
            "phase": self.phase or "",
            "owner_oid": self.owner_oid or "",
            "tenant_id": self.tenant_id or "",
            "task_id": self.task_id or "",
            "error_code": self.error_code or "",
            "created_at": self.created_at or "",
            "updated_at": self.updated_at or "",
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
        return cls(
            job_id=e["PartitionKey"],
            type=e.get("type", ""),
            status=e.get("status", ""),
            phase=e.get("phase") or None,
            owner_oid=e.get("owner_oid") or None,
            tenant_id=e.get("tenant_id") or None,
            task_id=e.get("task_id") or None,
            error_code=e.get("error_code") or None,
            created_at=e.get("created_at") or None,
            updated_at=e.get("updated_at") or None,
            payload=payload,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        return TableClient(
            endpoint=self._endpoint, table_name="jobstate", credential=self._cred
        )

    def _history_client(self) -> TableClient:
        return TableClient(
            endpoint=self._endpoint, table_name="jobhistory", credential=self._cred
        )

    # --- jobstate ---

    def create(self, state: JobState) -> JobState:
        if not state.created_at:
            state.created_at = _now_iso()
        state.updated_at = state.created_at
        with self._state_client() as t:
            t.create_entity(state.to_entity())
        self.append_history(state.job_id, "created", {"status": state.status, "phase": state.phase})
        return state

    def get(self, job_id: str) -> JobState | None:
        with self._state_client() as t:
            try:
                e = t.get_entity(partition_key=job_id, row_key="current")
            except ResourceNotFoundError:
                return None
        return JobState.from_entity(dict(e))

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
            e["updated_at"] = _now_iso()
            t.update_entity(e, mode=UpdateMode.MERGE)
        self.append_history(
            job_id,
            "update",
            {"status": status, "phase": phase, "error_code": error_code},
        )
        return JobState.from_entity(e)

    def list_for_owner(self, owner_oid: str, limit: int = 50) -> list[JobState]:
        safe_oid = _sanitise_odata_value(owner_oid)
        with self._state_client() as t:
            entities = t.query_entities(
                f"owner_oid eq '{safe_oid}'", results_per_page=limit
            )
            rows = []
            for e in entities:
                rows.append(JobState.from_entity(dict(e)))
                if len(rows) >= limit:
                    break
        rows.sort(key=lambda r: r.created_at or "", reverse=True)
        return rows

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
            with self._history_client() as t:
                t.create_entity(entity)
        except Exception as exc:
            # History is best-effort — never fail the parent write because
            # the audit append failed.
            LOGGER.warning("append_history failed for %s: %s", job_id, exc)

    def get_history(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        safe_id = _sanitise_odata_value(job_id)
        with self._history_client() as t:
            entities = t.query_entities(
                f"PartitionKey eq '{safe_id}'", results_per_page=limit
            )
            rows = []
            for e in entities:
                rows.append(dict(e))
                if len(rows) >= limit:
                    break
        rows.sort(key=lambda r: r["RowKey"])
        return rows
