"""DB-ops audit helper — record prepare-db / shard / oracle actions.

Responsibility: Create a JobState row and a jobhistory event for each
    destructive or long-running DB-administration action so the existing
    /api/audit/log surface (which already paginates jobstate by owner)
    automatically picks up the new operations.
Edit boundaries: No HTTP or Azure SDK logic here — only the state-repo
    contract. Routes call this once per request; daemons can call again to
    record completion / failure.
Key entry points: ``record_db_op``, ``record_db_op_event``.
Risky contracts: ``job_type`` values are referenced by the SPA's audit table
    filter (``prepare_db`` / ``shard`` / ``oracle``); keep them stable.
Validation: ``uv run pytest -q api/tests/test_db_ops_audit.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from api.auth import CallerIdentity
from api.services.state_repo import JobState
from api.services.state_repo import get_state_repo

LOGGER = logging.getLogger(__name__)

# Synthetic job_id format: ``dbops:<op>:<account>:<db>:<ulid_like>``. The
# leading prefix lets the audit UI group / filter DB-administration events
# separately from BLAST / warmup jobs without changing the JobState schema.
_DBOPS_PREFIX = "dbops:"


def _job_id(op: str, account: str, db_name: str) -> str:
    import uuid

    return f"{_DBOPS_PREFIX}{op}:{account}:{db_name}:{uuid.uuid4().hex[:12]}"


def record_db_op(
    *,
    op: str,
    caller: CallerIdentity,
    account_name: str,
    db_name: str,
    extra: dict[str, Any] | None = None,
) -> str:
    """Create a JobState row for a DB-admin action and return its job_id.

    Best-effort — failures are logged and we still return a synthetic id so
    callers can use it to correlate downstream history events. The route
    should NEVER fail because audit recording failed.
    """
    job_id = _job_id(op, account_name, db_name)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload: dict[str, Any] = {
        "op": op,
        "account_name": account_name,
        "db_name": db_name,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    try:
        repo = get_state_repo()
        repo.create(
            JobState(
                job_id=job_id,
                type=op,
                status="queued",
                phase="queued",
                owner_oid=caller.object_id,
                tenant_id=caller.tenant_id,
                created_at=now,
                updated_at=now,
                payload=payload,
            )
        )
        repo.append_history(job_id, "started", payload)
    except Exception as exc:
        LOGGER.warning("db-ops audit create failed op=%s db=%s: %s", op, db_name, exc)
    return job_id


def record_db_op_event(
    job_id: str,
    event: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append an event to a previously-created DB-op JobState.

    Safe to call from background daemons. Event names follow the existing
    audit convention (``completed`` / ``failed`` / ``partial``).
    """
    try:
        get_state_repo().append_history(job_id, event, payload or {})
    except Exception as exc:
        LOGGER.warning("db-ops audit event failed job=%s event=%s: %s", job_id, event, exc)


__all__ = ["record_db_op", "record_db_op_event"]
