"""Durable Storage state for DB order-oracle builds.

Responsibility: Claim, update, promote, fail, and release order-oracle build
    documents using Azure Blob ETag conditions.
Edit boundaries: JSON control-blob I/O and state transitions only; readiness,
    Kubernetes dispatch/polling, Celery orchestration, and HTTP shaping belong
    to their owning modules.
Key entry points: `claim_oracle_build`, `update_oracle_run`,
    `claim_oracle_execution`, `promote_oracle_run`, `fail_oracle_run`,
    `read_oracle_current`, `read_oracle_active`.
Risky contracts: Missing documents are created with `overwrite=False`; every
    replacement/deletion uses `IfNotModified`; run updates require the exact
    `owner_operation_id`; current-ready state is never replaced at build start.
Validation: `uv run pytest -q api/tests/test_oracle_state.py`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import ContentSettings

from api.services.db.order_oracle import (
    ORACLE_PARTS_DIR,
    ORACLE_PREFIX_ROOT,
    oracle_active_blob_path,
    oracle_automation_blob_path,
    oracle_run_status_blob_path,
    oracle_status_blob_path,
)
from api.services.env import env_int

_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_CAS_ATTEMPTS = env_int("ORACLE_STATE_CAS_MAX_ATTEMPTS", 8, minimum=1, maximum=20)
LOGGER = logging.getLogger(__name__)


class OracleStateConflict(RuntimeError):
    """A competing owner changed the active or run document."""


class OracleBuildInProgress(OracleStateConflict):
    """A different oracle identity is already being built for this DB."""


class OracleBuildOwnershipLost(OracleStateConflict):
    """The caller no longer owns the active oracle build."""


@dataclass(frozen=True, slots=True)
class OracleClaimResult:
    outcome: str
    document: dict[str, Any]


def oracle_container(credential: Any, account_name: str) -> Any:
    from api.services.storage.data import _blob_service

    return _blob_service(credential, account_name).get_container_client("blast-db")


def _read_document(container: Any, path: str) -> tuple[dict[str, Any] | None, str]:
    blob = container.get_blob_client(path)
    try:
        stream = blob.download_blob(offset=0, length=_MAX_DOCUMENT_BYTES + 1)
        raw = stream.readall()
    except ResourceNotFoundError:
        return None, ""
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"oracle state document exceeds {_MAX_DOCUMENT_BYTES} bytes")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("oracle state document is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("oracle state document must be a JSON object")
    etag = ""
    properties = getattr(stream, "properties", None)
    if properties is not None:
        etag = str(getattr(properties, "etag", "") or "")
    if not etag:
        properties = blob.get_blob_properties()
        etag = str(getattr(properties, "etag", "") or "")
    if not etag:
        raise RuntimeError("existing oracle state document is missing an ETag")
    return parsed, etag


def _write_document(
    container: Any,
    path: str,
    document: dict[str, Any],
    *,
    etag: str = "",
) -> str:
    blob = container.get_blob_client(path)
    kwargs: dict[str, Any]
    if etag:
        kwargs = {
            "overwrite": True,
            "etag": etag,
            "match_condition": MatchConditions.IfNotModified,
        }
    else:
        kwargs = {"overwrite": False}
    result = blob.upload_blob(
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        content_settings=ContentSettings(content_type="application/json; charset=utf-8"),
        **kwargs,
    )
    result_etag = str(getattr(result, "etag", "") or "")
    if not result_etag and isinstance(result, dict):
        result_etag = str(result.get("etag") or "")
    return result_etag.strip('"')


def _delete_document(container: Any, path: str, *, etag: str) -> None:
    container.get_blob_client(path).delete_blob(
        etag=etag,
        match_condition=MatchConditions.IfNotModified,
    )


def read_oracle_current(container: Any, db_name: str) -> dict[str, Any] | None:
    document, _etag = _read_document(container, oracle_status_blob_path(db_name))
    return document


def read_oracle_active(container: Any, db_name: str) -> dict[str, Any] | None:
    document, _etag = _read_document(container, oracle_active_blob_path(db_name))
    return document


def read_oracle_automation(container: Any, db_name: str) -> dict[str, Any]:
    document, _etag = _read_document(container, oracle_automation_blob_path(db_name))
    return document or {
        "schema_version": 1,
        "db_name": db_name,
        "status": "idle",
        "failure_count": 0,
        "retry_exhausted": False,
    }


def update_oracle_automation(
    container: Any,
    *,
    db_name: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    return mutate_oracle_automation(
        container,
        db_name=db_name,
        mutator=lambda _current: updates,
    )


def mutate_oracle_automation(
    container: Any,
    *,
    db_name: str,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """CAS-update automation state, recomputing changes after every race."""
    path = oracle_automation_blob_path(db_name)
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        current, etag = _read_document(container, path)
        current_document = current or {
            "schema_version": 1,
            "db_name": db_name,
            "status": "idle",
            "failure_count": 0,
            "retry_exhausted": False,
        }
        next_document = {
            "schema_version": 1,
            "db_name": db_name,
            **current_document,
            **mutator(dict(current_document)),
        }
        try:
            _write_document(container, path, next_document, etag=etag)
            return next_document
        except (ResourceExistsError, ResourceModifiedError):
            continue
    raise OracleStateConflict("oracle automation state CAS retries exhausted")


def read_oracle_run(container: Any, db_name: str, run_id: str) -> dict[str, Any] | None:
    document, _etag = _read_document(container, oracle_run_status_blob_path(db_name, run_id))
    return document


def claim_oracle_build(
    container: Any,
    *,
    db_name: str,
    document: dict[str, Any],
) -> OracleClaimResult:
    """Atomically claim one DB for an oracle build or adopt an identical run."""
    identity = str(document.get("identity") or "")
    run_id = str(document.get("run_id") or "")
    owner = str(document.get("owner_operation_id") or "")
    if not identity or not run_id or not owner:
        raise ValueError("identity, run_id, and owner_operation_id are required")

    current = read_oracle_current(container, db_name)
    if (
        isinstance(current, dict)
        and current.get("status") == "ready"
        and str(current.get("identity") or "") == identity
    ):
        return OracleClaimResult("ready", current)

    active_path = oracle_active_blob_path(db_name)
    try:
        _write_document(container, active_path, document)
    except ResourceExistsError:
        active, _etag = _read_document(container, active_path)
        if active is None:
            raise OracleStateConflict("oracle active claim raced with deletion") from None
        if str(active.get("identity") or "") == identity:
            return OracleClaimResult("adopted", active)
        raise OracleBuildInProgress(f"oracle build already active for {db_name}") from None

    run_path = oracle_run_status_blob_path(db_name, run_id)
    try:
        _write_document(container, run_path, document)
    except Exception:
        active, active_etag = _read_document(container, active_path)
        if active is not None and str(active.get("owner_operation_id") or "") == owner:
            try:
                _delete_document(container, active_path, etag=active_etag)
            except (ResourceModifiedError, ResourceNotFoundError):
                pass
        raise
    return OracleClaimResult("created", document)


def _require_owner(document: dict[str, Any], owner_operation_id: str) -> None:
    if str(document.get("owner_operation_id") or "") != owner_operation_id:
        raise OracleBuildOwnershipLost("oracle build ownership changed")


def update_oracle_run(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    owner_operation_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    path = oracle_run_status_blob_path(db_name, run_id)
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        current, etag = _read_document(container, path)
        if current is None:
            raise OracleBuildOwnershipLost("oracle run status no longer exists")
        _require_owner(current, owner_operation_id)
        next_document = {**current, **updates}
        try:
            _write_document(container, path, next_document, etag=etag)
            return next_document
        except ResourceModifiedError:
            continue
    raise OracleStateConflict("oracle run status CAS retries exhausted")


def update_oracle_active(
    container: Any,
    *,
    db_name: str,
    owner_operation_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    path = oracle_active_blob_path(db_name)
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        current, etag = _read_document(container, path)
        if current is None:
            raise OracleBuildOwnershipLost("oracle active claim no longer exists")
        _require_owner(current, owner_operation_id)
        next_document = {**current, **updates}
        try:
            _write_document(container, path, next_document, etag=etag)
            return next_document
        except ResourceModifiedError:
            continue
    raise OracleStateConflict("oracle active claim CAS retries exhausted")


def claim_oracle_execution(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    owner_operation_id: str,
    dispatch_token: str,
    execution_instance_id: str,
    started_at: str,
    deadline_at: str,
) -> bool:
    """Claim one delivered task instance; stale/duplicate deliveries no-op."""
    path = oracle_active_blob_path(db_name)
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        current, etag = _read_document(container, path)
        if current is None:
            return False
        _require_owner(current, owner_operation_id)
        if str(current.get("run_id") or "") != run_id:
            return False
        if str(current.get("dispatch_token") or "") != dispatch_token:
            return False
        if str(current.get("execution_instance_id") or ""):
            return False
        next_document = {
            **current,
            "execution_instance_id": execution_instance_id,
            "execution_started_at": started_at,
            "deadline_at": deadline_at,
            "status": "running",
            "phase": "starting",
            "updated_at": started_at,
        }
        try:
            _write_document(container, path, next_document, etag=etag)
            return True
        except ResourceModifiedError:
            continue
    raise OracleStateConflict("oracle execution claim CAS retries exhausted")


def release_oracle_active(
    container: Any,
    *,
    db_name: str,
    owner_operation_id: str,
) -> bool:
    path = oracle_active_blob_path(db_name)
    active, etag = _read_document(container, path)
    if active is None:
        return False
    _require_owner(active, owner_operation_id)
    try:
        _delete_document(container, path, etag=etag)
        return True
    except ResourceNotFoundError:
        return False
    except ResourceModifiedError as exc:
        raise OracleBuildOwnershipLost("oracle active claim changed") from exc


def promote_oracle_run(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    owner_operation_id: str,
    ready_document: dict[str, Any],
    release_active: bool = True,
) -> dict[str, Any]:
    """Publish a ready pointer without exposing a partially built run."""
    active, _active_etag = _read_document(container, oracle_active_blob_path(db_name))
    if active is None:
        raise OracleBuildOwnershipLost("oracle active claim no longer exists")
    _require_owner(active, owner_operation_id)
    if str(active.get("run_id") or "") != run_id:
        raise OracleBuildOwnershipLost("oracle active run changed")

    run, _run_etag = _read_document(container, oracle_run_status_blob_path(db_name, run_id))
    if run is None:
        raise OracleBuildOwnershipLost("oracle run status no longer exists")
    _require_owner(run, owner_operation_id)
    terminal = {**run, **ready_document, "status": "ready"}
    expected_parts = int(terminal.get("expected_parts") or 0)
    ready_parts = int(terminal.get("ready_parts") or 0)
    expected_shards = terminal.get("expected_shards")
    expected_prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_PARTS_DIR}/{run_id}/"
    if (
        terminal.get("schema_version") != 1
        or str(terminal.get("db_name") or "") != db_name
        or str(terminal.get("run_id") or "") != run_id
        or not str(terminal.get("identity") or "")
        or not str(terminal.get("source_version") or "")
        or not isinstance(expected_shards, list)
        or len(expected_shards) != expected_parts
        or len(set(str(shard) for shard in expected_shards)) != expected_parts
        or ready_parts != expected_parts
        or str(terminal.get("part_prefix") or "") != expected_prefix
        or not str(terminal.get("finished_at") or "")
    ):
        raise ValueError("oracle ready document is incomplete or inconsistent")
    current_path = oracle_status_blob_path(db_name)
    published = terminal
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        _current, current_etag = _read_document(container, current_path)
        current_run_id = str((_current or {}).get("run_id") or "")
        previous_run_id = (
            current_run_id
            if current_run_id and current_run_id != run_id
            else str((_current or {}).get("previous_run_id") or "")
        )
        published = {**terminal, "previous_run_id": previous_run_id}
        try:
            _write_document(container, current_path, published, etag=current_etag)
            break
        except (ResourceExistsError, ResourceModifiedError):
            continue
    else:
        raise OracleStateConflict("oracle current pointer CAS retries exhausted")

    try:
        update_oracle_run(
            container,
            db_name=db_name,
            run_id=run_id,
            owner_operation_id=owner_operation_id,
            updates={**ready_document, "status": "ready"},
        )
    except Exception as exc:
        LOGGER.warning(
            "oracle run history ready update skipped run_id=%s reason=%s",
            run_id,
            type(exc).__name__,
        )
        raise OracleStateConflict("oracle current published; run history recovery pending") from exc
    if release_active:
        try:
            release_oracle_active(
                container,
                db_name=db_name,
                owner_operation_id=owner_operation_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle published active release skipped run_id=%s reason=%s",
                run_id,
                type(exc).__name__,
            )
            raise OracleStateConflict(
                "oracle current published; active release recovery pending"
            ) from exc
    return published


def fail_oracle_run(
    container: Any,
    *,
    db_name: str,
    run_id: str,
    owner_operation_id: str,
    error_code: str,
    error: str,
    finished_at: str,
) -> dict[str, Any]:
    failed = update_oracle_run(
        container,
        db_name=db_name,
        run_id=run_id,
        owner_operation_id=owner_operation_id,
        updates={
            "status": "failed",
            "phase": "failed",
            "error_code": error_code,
            "error": error[:300],
            "finished_at": finished_at,
        },
    )
    try:
        release_oracle_active(
            container,
            db_name=db_name,
            owner_operation_id=owner_operation_id,
        )
    except OracleBuildOwnershipLost:
        pass
    return failed


__all__ = [
    "OracleBuildInProgress",
    "OracleBuildOwnershipLost",
    "OracleClaimResult",
    "OracleStateConflict",
    "claim_oracle_build",
    "claim_oracle_execution",
    "fail_oracle_run",
    "mutate_oracle_automation",
    "oracle_container",
    "promote_oracle_run",
    "read_oracle_active",
    "read_oracle_automation",
    "read_oracle_current",
    "read_oracle_run",
    "release_oracle_active",
    "update_oracle_active",
    "update_oracle_automation",
    "update_oracle_run",
]
