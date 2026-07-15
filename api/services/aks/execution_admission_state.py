"""Durable per-cluster AKS execution-admission lifecycle state.

Responsibility: Persist immutable lifecycle generations and their token-scoped ARM completion,
    cancellation, and database warmup correlation records.
Edit boundaries: No ARM, Kubernetes, JobState readiness, queue receive, or lifecycle side effects;
    decision policy belongs in `execution_admission.py`.
Key entry points: `create_lifecycle_barrier`, `get_lifecycle_barrier`,
    `cancel_lifecycle_barrier`, `record_lifecycle_completed`,
    `record_lifecycle_failed`, `lifecycle_failure`,
    `record_barrier_warmup_jobs`, `clear_barrier_warmup_job`,
    `get_barrier_warmup_jobs`,
    `lifecycle_barrier_interrupts_job`.
Risky contracts: Deployed writes fail closed unless Azure Table persistence succeeds. Token-scoped
    records never mutate another lifecycle generation, and per-database keys prevent concurrent
    warmup writers from losing each other's updates. Deployed reads distinguish confirmed missing
    rows from Table failures so a storage outage cannot silently open execution admission.
Validation: `uv run pytest -q api/tests/test_execution_admission.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api.services.state.singletons import (
    clear_singleton,
    load_singleton,
    load_singleton_strict,
    save_singleton,
)

LOGGER = logging.getLogger(__name__)

_BARRIER_PREFIX = "execution-admission-cluster-"
_WARMUP_PREFIX = "execution-admission-warmup-"
_CANCEL_PREFIX = "execution-admission-cancel-"
_COMPLETE_PREFIX = "execution-admission-complete-"
_FAILURE_PREFIX = "execution-admission-failure-"
_VALID_ACTIONS = frozenset({"start", "scale", "stop", "delete"})
_MEMORY_MAX_ENTRIES = max(
    128, int(os.environ.get("EXECUTION_ADMISSION_MEMORY_MAX_ENTRIES", "2048"))
)

_MEMORY: dict[str, dict[str, Any]] = {}
_MEMORY_LOCK = threading.Lock()


class ExecutionAdmissionPersistenceError(RuntimeError):
    """Raised when a deployed lifecycle barrier cannot be persisted durably."""


@dataclass(frozen=True)
class LifecycleBarrier:
    """One immutable lifecycle generation for a cluster."""

    token: str
    action: str
    subscription_id: str
    resource_group: str
    cluster_name: str
    target_node_count: int
    databases: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "action": self.action,
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "cluster_name": self.cluster_name,
            "target_node_count": self.target_node_count,
            "databases": list(self.databases),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LifecycleBarrier | None:
        token = str(value.get("token") or "").strip()
        action = str(value.get("action") or "").strip().lower()
        subscription_id = str(value.get("subscription_id") or "").strip()
        resource_group = str(value.get("resource_group") or "").strip()
        cluster_name = str(value.get("cluster_name") or "").strip()
        if (
            not token
            or action not in _VALID_ACTIONS
            or not all((subscription_id, resource_group, cluster_name))
        ):
            return None
        try:
            target = max(0, int(value.get("target_node_count") or 0))
        except (TypeError, ValueError):
            target = 0
        databases = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in value.get("databases", []) or []
                    if str(item).strip()
                }
            )
        )
        return cls(
            token=token,
            action=action,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            target_node_count=target,
            databases=databases,
            created_at=str(value.get("created_at") or ""),
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _context_digest(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    raw = "|".join(
        (
            subscription_id.strip().lower(),
            resource_group.strip().lower(),
            cluster_name.strip().lower(),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _barrier_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    return _BARRIER_PREFIX + _context_digest(subscription_id, resource_group, cluster_name)


def _warmup_key(token: str, database: str = "") -> str:
    suffix = hashlib.sha256(database.encode("utf-8")).hexdigest()[:16] if database else ""
    return _WARMUP_PREFIX + token + (f"-{suffix}" if suffix else "")


def _cancel_key(token: str) -> str:
    return _CANCEL_PREFIX + token


def _complete_key(token: str) -> str:
    return _COMPLETE_PREFIX + token


def _failure_key(token: str) -> str:
    return _FAILURE_PREFIX + token


def _redis_client() -> Any | None:
    try:
        from api.services.redis_clients import get_broker_redis_client

        return get_broker_redis_client(socket_timeout=2)
    except Exception:
        return None


def _cache_payload(key: str, payload: dict[str, Any]) -> None:
    with _MEMORY_LOCK:
        if key not in _MEMORY and len(_MEMORY) >= _MEMORY_MAX_ENTRIES:
            _MEMORY.pop(next(iter(_MEMORY)))
        _MEMORY[key] = dict(payload)
    client = _redis_client()
    if client is None:
        return
    try:
        client.set(key, json.dumps(payload, separators=(",", ":"), default=str))
    except Exception as exc:
        LOGGER.warning(
            "execution admission Redis write failed key=%s error=%s; "
            "durable Table fallback remains active",
            key,
            type(exc).__name__,
        )


def _persist(key: str, payload: dict[str, Any]) -> None:
    durable = save_singleton(key, payload)
    if os.environ.get("CONTAINER_APP_NAME") and not durable:
        raise ExecutionAdmissionPersistenceError(
            "execution admission barrier could not be persisted to Azure Table Storage"
        )
    _cache_payload(key, payload)


def _load(key: str) -> dict[str, Any] | None:
    client = _redis_client()
    if client is not None:
        try:
            raw = client.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if isinstance(raw, str) and raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return dict(parsed)
        except Exception:
            LOGGER.debug("execution admission Redis read failed key=%s", key, exc_info=True)
    if os.environ.get("CONTAINER_APP_NAME"):
        try:
            durable = load_singleton_strict(key)
        except Exception as exc:
            raise ExecutionAdmissionPersistenceError(
                "execution admission state could not be read from Azure Table Storage"
            ) from exc
    else:
        durable = load_singleton(key)
    if durable is not None:
        normalised = dict(durable)
        _cache_payload(key, normalised)
        return normalised
    if os.environ.get("CONTAINER_APP_NAME"):
        return None
    with _MEMORY_LOCK:
        cached = _MEMORY.get(key)
        return dict(cached) if cached is not None else None


def _remove(key: str) -> None:
    """Best-effort removal of one superseded token-scoped record."""
    clear_singleton(key)
    with _MEMORY_LOCK:
        _MEMORY.pop(key, None)
    client = _redis_client()
    if client is not None:
        try:
            client.delete(key)
        except Exception:
            LOGGER.debug("execution admission Redis delete failed key=%s", key, exc_info=True)


def create_lifecycle_barrier(
    *,
    action: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    target_node_count: int = 0,
    databases: list[str] | tuple[str, ...] | None = None,
    token: str = "",
) -> LifecycleBarrier:
    """Persist a new lifecycle generation before its side effect is enqueued."""
    action = action.strip().lower()
    if action not in _VALID_ACTIONS:
        raise ValueError(f"unsupported lifecycle action: {action}")
    if not subscription_id or not resource_group or not cluster_name:
        raise ValueError("subscription_id, resource_group, and cluster_name are required")
    previous = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
    barrier = LifecycleBarrier(
        token=token.strip() or uuid.uuid4().hex,
        action=action,
        subscription_id=subscription_id.strip(),
        resource_group=resource_group.strip(),
        cluster_name=cluster_name.strip(),
        target_node_count=max(0, int(target_node_count or 0)),
        databases=tuple(
            sorted({str(item).strip() for item in databases or [] if str(item).strip()})
        ),
        created_at=_now_iso(),
    )
    _persist(_barrier_key(subscription_id, resource_group, cluster_name), barrier.to_dict())
    if previous is not None and previous.token != barrier.token:
        for database in previous.databases:
            _remove(_warmup_key(previous.token, database))
        _remove(_cancel_key(previous.token))
        _remove(_complete_key(previous.token))
        _remove(_failure_key(previous.token))
    LOGGER.info(
        "execution admission barrier created cluster=%s action=%s token=%s "
        "target_nodes=%d databases=%d",
        cluster_name,
        action,
        barrier.token[:12],
        barrier.target_node_count,
        len(barrier.databases),
    )
    return barrier


def get_lifecycle_barrier(
    subscription_id: str, resource_group: str, cluster_name: str
) -> LifecycleBarrier | None:
    payload = _load(_barrier_key(subscription_id, resource_group, cluster_name))
    if payload is None:
        return None
    barrier = LifecycleBarrier.from_dict(payload)
    if barrier is None:
        LOGGER.warning("invalid execution admission barrier cluster=%s", cluster_name)
    return barrier


def cancel_lifecycle_barrier(token: str, *, reason: str) -> None:
    """Cancel only the named generation after its lifecycle enqueue failed."""
    if not token:
        return
    _persist(
        _cancel_key(token),
        {"token": token, "reason": reason[:128], "cancelled_at": _now_iso()},
    )


def barrier_cancelled(token: str) -> bool:
    return bool(token and _load(_cancel_key(token)) is not None)


def record_lifecycle_completed(
    *,
    token: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> bool:
    """Persist ARM lifecycle convergence for the matching immutable generation."""
    if not token:
        return False
    barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
    if barrier is None or barrier.token != token or barrier_cancelled(token):
        return False
    _persist(
        _complete_key(token),
        {
            "token": token,
            "action": barrier.action,
            "completed_at": _now_iso(),
        },
    )
    return True


def lifecycle_completed(token: str) -> bool:
    payload = _load(_complete_key(token)) if token else None
    return bool(payload is not None and str(payload.get("token") or "") == token)


def record_lifecycle_failed(
    *,
    token: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    error_code: str,
) -> bool:
    """Persist a terminal lifecycle-task failure for the matching generation."""
    if not token:
        return False
    barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
    if barrier is None or barrier.token != token or barrier_cancelled(token):
        return False
    _persist(
        _failure_key(token),
        {
            "token": token,
            "action": barrier.action,
            "error_code": error_code[:128],
            "failed_at": _now_iso(),
        },
    )
    return True


def lifecycle_failure(token: str) -> dict[str, Any] | None:
    """Return the terminal failure marker for one lifecycle generation."""
    payload = _load(_failure_key(token)) if token else None
    if payload is None or str(payload.get("token") or "") != token:
        return None
    return payload


def record_barrier_warmup_jobs(
    *,
    token: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    jobs: dict[str, str],
) -> bool:
    """Write independent warmup Job IDs for the matching lifecycle generation."""
    if not token or not jobs:
        return False
    barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
    if barrier is None or barrier.token != token or barrier_cancelled(token):
        return False
    recorded = False
    required = set(barrier.databases)
    for name, job_id in jobs.items():
        name = str(name).strip()
        job_id = str(job_id).strip()
        if not name or not job_id or name not in required:
            continue
        _persist(
            _warmup_key(token, name),
            {
                "token": token,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "database": name,
                "job_id": job_id,
                "updated_at": _now_iso(),
            },
        )
        recorded = True
    return recorded


def clear_barrier_warmup_job(
    *,
    token: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    database: str,
) -> bool:
    """Remove one correlation only while ``token`` is the current generation.

    Used when a warmup broker enqueue fails after its fail-closed correlation
    was persisted. A superseded lifecycle generation can never delete the new
    generation's correlation.
    """
    barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
    if (
        not token
        or barrier is None
        or barrier.token != token
        or barrier_cancelled(token)
        or database not in barrier.databases
    ):
        return False
    _remove(_warmup_key(token, database))
    return True


def get_barrier_warmup_jobs(token: str, databases: tuple[str, ...] | list[str]) -> dict[str, str]:
    jobs: dict[str, str] = {}
    for database in databases:
        payload = _load(_warmup_key(token, database)) if token else None
        if payload is None or str(payload.get("token") or "") != token:
            continue
        job_id = str(payload.get("job_id") or "").strip()
        if job_id:
            jobs[database] = job_id
    return jobs


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def lifecycle_barrier_interrupts_job(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    job_created_at: str,
) -> LifecycleBarrier | None:
    """Return the newer lifecycle generation that can explain a lost external job."""
    barrier = get_lifecycle_barrier(subscription_id, resource_group, cluster_name)
    if barrier is None or barrier_cancelled(barrier.token):
        return None
    barrier_at = _parse_time(barrier.created_at)
    job_at = _parse_time(job_created_at)
    if barrier_at is None or job_at is None or barrier_at < job_at:
        return None
    return barrier


def reset_execution_admission_state_for_tests() -> None:
    """Clear process-local state; durable/Redis stores are monkeypatched in tests."""
    with _MEMORY_LOCK:
        _MEMORY.clear()


__all__ = [
    "ExecutionAdmissionPersistenceError",
    "LifecycleBarrier",
    "barrier_cancelled",
    "cancel_lifecycle_barrier",
    "clear_barrier_warmup_job",
    "create_lifecycle_barrier",
    "get_barrier_warmup_jobs",
    "get_lifecycle_barrier",
    "lifecycle_barrier_interrupts_job",
    "lifecycle_completed",
    "lifecycle_failure",
    "record_barrier_warmup_jobs",
    "record_lifecycle_completed",
    "record_lifecycle_failed",
    "reset_execution_admission_state_for_tests",
]
