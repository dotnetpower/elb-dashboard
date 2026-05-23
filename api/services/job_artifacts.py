"""Job artifact storage helpers for large BLAST UI data.

Responsibility: Job artifact storage helpers for large BLAST UI data
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `ArtifactState`, `_now_iso`, `_platform_storage_account_name`,
`write_json_artifact`, `read_json_artifact`, `upsert_artifact_state`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode
from azure.storage.blob import BlobServiceClient

from api.services import get_credential
from api.services.storage import data as storage_data

LOGGER = logging.getLogger(__name__)

ARTIFACTS_CONTAINER = os.environ.get("JOB_ARTIFACTS_CONTAINER", "job-artifacts")
ARTIFACTS_TABLE = os.environ.get("JOB_ARTIFACTS_TABLE", "jobartifactstate")
_EXECUTION_STEPS_MAX_BYTES = int(os.environ.get("EXECUTION_STEPS_SNAPSHOT_MAX_BYTES", "4194304"))

# Process-shared pooled TableClient for the artifact state table — see
# ``_artifact_table_client`` for the contract.
_ARTIFACT_TABLE_POOLED: TableClient | None = None
_ARTIFACT_TABLE_POOL_LOCK = threading.Lock()
_ANALYTICS_JSON_MAX_BYTES = int(os.environ.get("RESULT_ANALYTICS_ARTIFACT_MAX_BYTES", "16777216"))
_PENDING_STALE_SECONDS = int(os.environ.get("JOB_ARTIFACT_PENDING_STALE_SECONDS", "900"))
_ENSURED_TABLES: set[tuple[str, str]] = set()
_ENSURED_TABLES_LOCK = threading.Lock()
_ENSURED_CONTAINERS: set[tuple[str, str]] = set()
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "deleted"}
_ANALYTICS_ARTIFACT_TYPES = {
    "result_manifest": "results/manifest.json",
    "result_aggregate": "results/aggregate.json",
    "result_alignments": "results/alignments.page.json",
    "result_taxonomy": "results/taxonomy.json",
}

# Minimum payload `artifact_schema_version` a baked analytics artifact
# must declare to be considered fresh. Bump the entry below — and the
# `artifact_schema_version` field emitted by the matching builder in
# `blast_result_artifacts.py` — whenever the payload semantics change.
# Artifacts with a lower or missing version are treated as stale, the
# state row is flipped to `failed` so `artifact_build_should_enqueue`
# returns True, and the next request triggers a rebuild.
#
# 2026-05-22 v2 — `rollup_taxonomy` gained the stitle organism
# fallback and `build_default_taxonomy_payload` now enriches with
# lineage / blast_name. Older "ready" artifacts must be rebuilt.
#
# IMPORTANT: a builder whose minimum is N MUST stamp its payload with
# `"artifact_schema_version": N` (or higher), otherwise the bake would
# write a payload that the next read immediately marks stale → infinite
# rebuild loop. Builders for types with minimum 0 may skip the stamp.
_ANALYTICS_ARTIFACT_MIN_SCHEMA_VERSION = {
    "result_manifest": 0,
    "result_aggregate": 0,
    "result_alignments": 2,
    "result_taxonomy": 2,
}


@dataclass(frozen=True)
class ArtifactState:
    job_id: str
    artifact_type: str
    status: str
    blob_path: str = ""
    content_hash: str = ""
    size_bytes: int = 0
    updated_at: str = ""
    error_code: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _platform_storage_account_name() -> str:
    endpoint = os.environ.get("AZURE_BLOB_ENDPOINT", "").strip()
    if endpoint:
        host = urlparse(endpoint).netloc or endpoint.removeprefix("https://").split("/", 1)[0]
        account = host.split(".", 1)[0]
        if account:
            return account
    fallback = os.environ.get("AZURE_STORAGE_ACCOUNT") or os.environ.get("STORAGE_ACCOUNT_NAME")
    if fallback:
        return fallback.strip()
    raise RuntimeError("AZURE_BLOB_ENDPOINT is not set")


def _table_endpoint() -> str:
    endpoint = os.environ.get("AZURE_TABLE_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("AZURE_TABLE_ENDPOINT is not set")
    return endpoint


def _ensure_artifacts_table(endpoint: str) -> None:
    key = (endpoint, ARTIFACTS_TABLE)
    if key in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if key in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(ARTIFACTS_TABLE)
            except AttributeError:
                try:
                    service.create_table(ARTIFACTS_TABLE)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(key)


def _artifact_table_client() -> TableClient:
    """Return a process-shared pooled ``TableClient`` for the artifacts table.

    Each call previously instantiated a fresh ``TableClient`` (and a fresh
    azure-core HTTP pipeline) that the ``with`` block closed on exit. On
    every BLAST submit the artifact write path was paying a full TLS
    handshake. Pool a single client so the HTTP session is reused, with
    :class:`state_repo._PooledTableClient` so ``with table:`` keeps working
    without tearing down the transport.
    """
    endpoint = _table_endpoint()
    _ensure_artifacts_table(endpoint)
    global _ARTIFACT_TABLE_POOLED
    pool = _ARTIFACT_TABLE_POOLED
    if pool is not None:
        return pool  # type: ignore[return-value]
    with _ARTIFACT_TABLE_POOL_LOCK:
        if _ARTIFACT_TABLE_POOLED is None:
            from api.services.state_repo import _PooledTableClient

            _ARTIFACT_TABLE_POOLED = _PooledTableClient(  # type: ignore[assignment]
                TableClient(
                    endpoint=endpoint,
                    table_name=ARTIFACTS_TABLE,
                    credential=get_credential(),
                )
            )
        return _ARTIFACT_TABLE_POOLED  # type: ignore[return-value]


def _reset_artifact_table_pool() -> None:
    """Test hook + safety valve for credential reset."""
    global _ARTIFACT_TABLE_POOLED
    with _ARTIFACT_TABLE_POOL_LOCK:
        pool = _ARTIFACT_TABLE_POOLED
        _ARTIFACT_TABLE_POOLED = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_artifacts_container(account_name: str) -> None:
    key = (account_name, ARTIFACTS_CONTAINER)
    if key in _ENSURED_CONTAINERS:
        return
    from api.services.storage.endpoint import blob_account_url

    client = BlobServiceClient(
        account_url=blob_account_url(account_name),
        credential=get_credential(),
        retry_total=0,
        connection_timeout=5,
        read_timeout=10,
    ).get_container_client(ARTIFACTS_CONTAINER)
    try:
        client.create_container()
    except ResourceExistsError:
        pass
    _ENSURED_CONTAINERS.add(key)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _job_artifact_path(job_id: str, suffix: str) -> str:
    if not job_id or "/" in job_id or ".." in job_id:
        raise ValueError("invalid job_id")
    suffix = suffix.strip("/")
    if not suffix or ".." in suffix or suffix.startswith("/"):
        raise ValueError("invalid artifact suffix")
    return f"{job_id}/{suffix}"


def write_json_artifact(
    job_id: str,
    artifact_type: str,
    suffix: str,
    payload: dict[str, Any],
    *,
    gzip_content: bool = False,
) -> ArtifactState:
    """Write a JSON artifact and update its tiny Table index row."""
    raw = _json_bytes(payload)
    body = gzip.compress(raw) if gzip_content else raw
    content_hash = _hash_bytes(raw)
    blob_path = _job_artifact_path(job_id, suffix + (".gz" if gzip_content else ""))
    account = _platform_storage_account_name()
    upsert_artifact_state(
        job_id,
        artifact_type,
        status="pending",
        blob_path=blob_path,
        content_hash=content_hash,
        size_bytes=len(body),
    )
    try:
        _ensure_artifacts_container(account)
        storage_data.upload_blob_bytes(
            get_credential(),
            account,
            ARTIFACTS_CONTAINER,
            blob_path,
            body,
            content_type="application/gzip" if gzip_content else "application/json; charset=utf-8",
        )
    except Exception as exc:
        try:
            upsert_artifact_state(
                job_id,
                artifact_type,
                status="failed",
                blob_path=blob_path,
                content_hash=content_hash,
                size_bytes=len(body),
                error_code=type(exc).__name__,
            )
        except Exception:
            LOGGER.debug("artifact failure state write failed", exc_info=True)
        raise
    return upsert_artifact_state(
        job_id,
        artifact_type,
        status="ready",
        blob_path=blob_path,
        content_hash=content_hash,
        size_bytes=len(body),
    )


def read_json_artifact(
    job_id: str,
    artifact_type: str,
    *,
    max_bytes: int = _ANALYTICS_JSON_MAX_BYTES,
) -> dict[str, Any] | None:
    state = get_artifact_state(job_id, artifact_type)
    if state is None or state.status != "ready" or not state.blob_path:
        return None
    account = _platform_storage_account_name()
    if state.blob_path.endswith(".gz"):
        compressed = b"".join(
            storage_data.stream_blob_bytes(
                get_credential(),
                account,
                ARTIFACTS_CONTAINER,
                state.blob_path,
            )
        )
        text = gzip.decompress(compressed).decode("utf-8", errors="replace")[:max_bytes]
    else:
        text = storage_data.read_blob_text(
            get_credential(),
            account,
            ARTIFACTS_CONTAINER,
            state.blob_path,
            max_bytes=max_bytes,
        )
    try:
        return cast(dict[str, Any], json.loads(text))
    except json.JSONDecodeError:
        LOGGER.warning("invalid JSON artifact job_id=%s type=%s", job_id, artifact_type)
        return None


def upsert_artifact_state(
    job_id: str,
    artifact_type: str,
    *,
    status: str,
    blob_path: str = "",
    content_hash: str = "",
    size_bytes: int = 0,
    error_code: str = "",
) -> ArtifactState:
    updated_at = _now_iso()
    entity = {
        "PartitionKey": job_id,
        "RowKey": artifact_type,
        "status": status,
        "blob_path": blob_path,
        "content_hash": content_hash,
        "size_bytes": int(size_bytes or 0),
        "updated_at": updated_at,
        "error_code": error_code,
    }
    with _artifact_table_client() as table:
        table.upsert_entity(entity, mode=UpdateMode.MERGE)
    return ArtifactState(
        job_id=job_id,
        artifact_type=artifact_type,
        status=status,
        blob_path=blob_path,
        content_hash=content_hash,
        size_bytes=int(size_bytes or 0),
        updated_at=updated_at,
        error_code=error_code,
    )


def get_artifact_state(job_id: str, artifact_type: str) -> ArtifactState | None:
    try:
        with _artifact_table_client() as table:
            entity = dict(table.get_entity(partition_key=job_id, row_key=artifact_type))
    except ResourceNotFoundError:
        return None
    return ArtifactState(
        job_id=job_id,
        artifact_type=artifact_type,
        status=str(entity.get("status") or ""),
        blob_path=str(entity.get("blob_path") or ""),
        content_hash=str(entity.get("content_hash") or ""),
        size_bytes=int(entity.get("size_bytes") or 0),
        updated_at=str(entity.get("updated_at") or ""),
        error_code=str(entity.get("error_code") or ""),
    )


def artifact_state_payload(job_id: str, artifact_type: str) -> dict[str, Any] | None:
    state = get_artifact_state(job_id, artifact_type)
    if state is None:
        return None
    return {
        "job_id": state.job_id,
        "artifact_type": state.artifact_type,
        "artifact_state": state.status,
        "blob_path": state.blob_path,
        "size_bytes": state.size_bytes,
        "updated_at": state.updated_at,
        "error_code": state.error_code or None,
    }


def artifact_build_should_enqueue(job_id: str, artifact_types: list[str]) -> bool:
    now = datetime.now(UTC)
    for artifact_type in artifact_types:
        state = get_artifact_state(job_id, artifact_type)
        if state is None or state.status == "failed":
            return True
        if state.status == "ready":
            continue
        if state.status == "pending":
            try:
                updated_at = datetime.fromisoformat(state.updated_at.replace("Z", "+00:00"))
            except Exception:
                return True
            if (now - updated_at).total_seconds() >= _PENDING_STALE_SECONDS:
                return True
            continue
        return True
    return False


def build_execution_steps_snapshot(state: Any) -> dict[str, Any]:
    state_payload = getattr(state, "payload", None)
    payload = state_payload if isinstance(state_payload, dict) else {}
    raw_progress = payload.get("_progress")
    progress: dict[str, Any] = raw_progress if isinstance(raw_progress, dict) else {}
    raw_steps = progress.get("steps")
    steps: dict[str, Any] = raw_steps if isinstance(raw_steps, dict) else {}
    return {
        "schema_version": 1,
        "job_id": str(getattr(state, "job_id", "")),
        "status": str(getattr(state, "status", "") or progress.get("status") or ""),
        "phase": str(getattr(state, "phase", "") or progress.get("phase") or ""),
        "created_at": getattr(state, "created_at", None),
        "updated_at": getattr(state, "updated_at", None),
        "custom_status": {
            "phase": progress.get("phase") or getattr(state, "phase", None),
            "status": progress.get("status") or getattr(state, "status", None),
            "steps": steps,
        },
        "output": {
            "status": getattr(state, "status", None),
            "phase": getattr(state, "phase", None),
            "steps": steps,
        },
        "artifact_state": "inline_fallback",
        "log_artifacts": {
            "container": ARTIFACTS_CONTAINER,
            "prefix": f"{getattr(state, 'job_id', '')}/execution-steps/logs/",
        },
    }


def write_execution_log_chunk(
    job_id: str,
    step_key: str,
    sequence: int,
    events: list[dict[str, Any]],
) -> ArtifactState | None:
    if not events:
        return None
    safe_step = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in step_key)[:80]
    payload = {
        "schema_version": 1,
        "job_id": job_id,
        "step": safe_step,
        "sequence": sequence,
        "events": events,
        "created_at": _now_iso(),
    }
    return write_json_artifact(
        job_id,
        f"execution_log_{safe_step}_{sequence:06d}",
        f"execution-steps/logs/{safe_step}/{sequence:06d}.json",
        payload,
    )


def write_execution_steps_snapshot(state: Any) -> ArtifactState | None:
    """Persist a terminal execution-steps snapshot.

    Returns None for non-terminal jobs. Exceptions are intentionally allowed to
    bubble to callers that want diagnostics; task hooks should catch and log.
    """
    status = str(getattr(state, "status", "") or "").casefold()
    if status not in _TERMINAL_STATUSES:
        return None
    payload = build_execution_steps_snapshot(state)
    payload["artifact_state"] = "ready"
    return write_json_artifact(
        str(getattr(state, "job_id", "")),
        "execution_steps",
        "execution-steps/current.json",
        payload,
    )


def read_execution_steps_snapshot(job_id: str) -> dict[str, Any] | None:
    return read_json_artifact(job_id, "execution_steps", max_bytes=_EXECUTION_STEPS_MAX_BYTES)


def analytics_artifact_suffix(artifact_type: str) -> str:
    try:
        return _ANALYTICS_ARTIFACT_TYPES[artifact_type]
    except KeyError as exc:
        raise ValueError(f"unknown analytics artifact type: {artifact_type}") from exc


def write_result_analytics_artifact(
    job_id: str,
    artifact_type: str,
    payload: dict[str, Any],
) -> ArtifactState:
    return write_json_artifact(
        job_id,
        artifact_type,
        analytics_artifact_suffix(artifact_type),
        payload,
    )


def read_result_analytics_artifact(job_id: str, artifact_type: str) -> dict[str, Any] | None:
    payload = read_json_artifact(job_id, artifact_type, max_bytes=_ANALYTICS_JSON_MAX_BYTES)
    if payload is None:
        return None
    minimum = _ANALYTICS_ARTIFACT_MIN_SCHEMA_VERSION.get(artifact_type, 1)
    raw_version = payload.get("artifact_schema_version")
    try:
        actual = int(raw_version) if raw_version is not None else 0
    except (TypeError, ValueError):
        actual = 0
    if actual < minimum:
        # Stale payload from a previous code version. Flip the state row
        # to `failed` so `artifact_build_should_enqueue` triggers a
        # rebuild on the next request, and behave as if the artifact
        # were missing for this read.
        try:
            upsert_artifact_state(
                job_id,
                artifact_type,
                status="failed",
                error_code="schema_stale",
            )
        except Exception:
            LOGGER.debug(
                "artifact stale state flip failed job_id=%s type=%s",
                job_id,
                artifact_type,
                exc_info=True,
            )
        LOGGER.info(
            "artifact schema stale job_id=%s type=%s actual=%s minimum=%s",
            job_id,
            artifact_type,
            actual,
            minimum,
        )
        return None
    return payload
