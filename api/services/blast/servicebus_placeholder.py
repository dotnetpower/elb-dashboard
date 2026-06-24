"""Service Bus request placeholder jobstate rows (queue-visible-from-send).

A BLAST request enqueued onto the Service Bus request queue is not picked up
until the next ``drain_and_resubmit`` beat tick (~30 s), and only then does a
durable jobstate row appear in Recent searches / the Message Flow card. This
module writes a lightweight ``queued`` placeholder row AT SEND TIME so the job
is visible the instant it lands on the queue, then the drain path supersedes it
with the real OpenAPI-keyed row.

Responsibility: Create a correlation-id-keyed ``queued`` placeholder JobState
    row (+ the ``enqueued`` message-flow trace stage) when a request is sent,
    and supersede / fail it once the consumer drains the message. All writes are
    best-effort — a placeholder failure must NEVER block the enqueue or abandon
    a drained message.
Edit boundaries: Reusable domain logic only — the send route (api sidecar) and
    the drain task (worker) both call this. No HTTP shaping, no Service Bus SDK,
    no Celery task bodies.
Key entry points: ``create_queued_placeholder``, ``supersede_placeholder``,
    ``fail_placeholder``.
Risky contracts: The placeholder ``job_id`` IS the ``external_correlation_id``
    (the only id known at send time). The real drained row is keyed by the
    OpenAPI ``job_id``; the two are distinct rows, so ``supersede_placeholder``
    soft-deletes the placeholder (``status='deleted'``) which the
    ``status ne 'deleted'`` list filter then hides — the real row carries the
    job forward. A placeholder is marked ``submission_source='servicebus'`` and
    ``owner_oid=''`` so it appears under the same subscription-scoped listing as
    the drained row. Never raises.
Validation: ``uv run pytest -q api/tests/test_servicebus_placeholder.py``.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)

# The placeholder lives only until the drain path creates the real OpenAPI-keyed
# row (typically one beat tick). It is intentionally minimal — just enough for
# the list view and the Message Flow card to show "queued" with the right db /
# program / scope.
_PLACEHOLDER_STATUS = "queued"
_PLACEHOLDER_PHASE = "queued"


def create_queued_placeholder(
    *,
    correlation_id: str,
    program: str,
    db: str,
    request_id: str = "",
    owner_oid: str = "",
    tenant_id: str = "",
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
    storage_account: str = "",
) -> bool:
    """Write a ``queued`` placeholder jobstate row keyed by ``correlation_id``.

    Returns ``True`` when the row was created (or already existed), ``False`` on
    any failure. Best-effort by contract: the caller (the send route) must treat
    a ``False`` as "the message is still enqueued, it just won't show as queued
    until the drain creates the real row" — never as a send failure.

    Idempotent: a duplicate send with the same ``correlation_id`` (at-least-once
    producer, user double-click) returns ``True`` without creating a second row
    because ``repo.create`` swallows ``ResourceExistsError`` and returns the
    existing row.
    """
    cid = str(correlation_id or "").strip()
    if not cid:
        return False
    try:
        from api.services.blast.message_trace import record_stage
        from api.services.state_repo import JobState, get_state_repo

        repo = get_state_repo()
        state = JobState(
            job_id=cid,
            type="blast",
            status=_PLACEHOLDER_STATUS,
            phase=_PLACEHOLDER_PHASE,
            owner_oid=owner_oid or "",
            owner_upn="api",
            tenant_id=tenant_id or "",
            program=program or "",
            db=db or "",
            subscription_id=subscription_id or "",
            resource_group=resource_group or "",
            cluster_name=cluster_name or "",
            storage_account=storage_account or "",
            payload={
                "submission_source": "servicebus",
                "external_correlation_id": cid,
                "placeholder": True,
                "request_id": request_id or "",
            },
        )
        repo.create(state)
        # Record the first message-flow stage so the Message Flow card shows the
        # job entering the queue immediately (not only after the drain tick).
        record_stage(repo, cid, "enqueued")
        return True
    except Exception as exc:  # best-effort — never block the enqueue
        LOGGER.info(
            "servicebus queued placeholder create skipped corr=%s: %s",
            cid,
            type(exc).__name__,
        )
        return False


def placeholder_exists(correlation_id: str) -> bool:
    """True when a send-time placeholder row exists for this correlation id.

    The control-plane send route (``POST /api/settings/service-bus/send``) writes
    this placeholder AT SEND TIME; an external producer that enqueues straight to
    the Service Bus namespace cannot write to the dashboard's jobstate table, so
    the presence of a placeholder is a spoof-resistant signal that the request
    came through the control plane. Best-effort: any read failure returns
    ``False`` (treated as "not control plane") so a transient Table blip never
    mislabels a job. A soft-deleted (superseded) placeholder still counts — it
    proves the row was created by the send route at some point.
    """
    cid = str(correlation_id or "").strip()
    if not cid:
        return False
    try:
        from api.services.state_repo import get_state_repo

        existing = get_state_repo().get(cid)
        if existing is None:
            return False
        payload = getattr(existing, "payload", None)
        return bool(isinstance(payload, dict) and payload.get("placeholder"))
    except Exception:  # pragma: no cover - best-effort, never raises into drain
        return False


def supersede_placeholder(correlation_id: str) -> None:
    """Soft-delete the placeholder once the real drained row exists.

    Called from the drain path right after the OpenAPI-keyed row is created.
    Soft-deletes (``status='deleted'``) so the ``status ne 'deleted'`` list
    filter hides it — the real row carries the job forward. Best-effort: a
    failure here leaves a stale ``queued`` placeholder that the real row
    duplicates in the list for one render, which the stale-job reconciler will
    eventually terminalise; it must NEVER abandon the drained message.
    """
    cid = str(correlation_id or "").strip()
    if not cid:
        return
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        existing = repo.get(cid)
        # Only supersede a row that is still our placeholder. A real job whose
        # job_id happens to equal the correlation_id (it never does — OpenAPI ids
        # are 12-hex, correlation ids are uuid4 hex / caller strings — but guard
        # anyway) must not be soft-deleted.
        if existing is None:
            return
        payload = existing.payload if isinstance(existing.payload, dict) else {}
        if not payload.get("placeholder"):
            return
        repo.update(cid, status="deleted", phase="deleted")
    except Exception as exc:  # best-effort
        LOGGER.info(
            "servicebus placeholder supersede skipped corr=%s: %s",
            cid,
            type(exc).__name__,
        )


def fail_placeholder(correlation_id: str, *, error_code: str) -> None:
    """Mark the placeholder ``failed`` when the message can never succeed.

    Called from the drain path when a message is dead-lettered (a permanent 4xx
    or an un-buildable payload). Without this the placeholder would linger as
    ``queued`` forever even though the message is in the DLQ. Best-effort.
    """
    cid = str(correlation_id or "").strip()
    if not cid:
        return
    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        existing = repo.get(cid)
        if existing is None:
            return
        payload = existing.payload if isinstance(existing.payload, dict) else {}
        if not payload.get("placeholder"):
            return
        repo.update(cid, status="failed", phase="failed", error_code=error_code[:200])
    except Exception as exc:  # best-effort
        LOGGER.info(
            "servicebus placeholder fail skipped corr=%s: %s",
            cid,
            type(exc).__name__,
        )
