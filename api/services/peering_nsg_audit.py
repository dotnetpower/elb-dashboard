"""Guaranteed terminal-event audit session for peering-NSG operator actions.

Responsibility: Wrap an ARM-mutating peering-NSG critical section with a start/terminal audit
row whose terminal event is guaranteed (or an `interrupted` row is emitted) even on raise.
Edit boundaries: Audit persistence lifecycle only — no HTTP parsing, no ARM/NSG calls, no
response shaping. The route owns request handling and orchestration and calls these helpers.
Key entry points: `record_audit_started`, `record_audit_event`, `audit_session`
Risky contracts: Audit failure must never break the underlying NSG operation — every backend
call is wrapped and swallowed (logged). `audit_session` MUST emit a terminal or `interrupted`
event for every started row so the Audit screen never shows a phantom in-flight row; the
`record_audit_event` bool return is what lets the session tell "backend down, skipped" from
"genuinely recorded".
Validation: `uv run pytest -q api/tests/test_peering_nsg_audit.py
api/tests/test_settings_vnet_peering.py api/tests/test_peering_nsg.py`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from api.auth import CallerIdentity

LOGGER = logging.getLogger(__name__)


def record_audit_started(
    *,
    op: str,
    caller: CallerIdentity,
    target_nsg_name: str,
    destination_ip: str,
    extra: dict[str, Any],
) -> str | None:
    """Audit start event. Returns the job_id, or None if the audit
    backend is unavailable (audit failure must never break the actual
    NSG operation).

    ``extra`` is merged into the JobState payload. We always inject a
    ``category="peering-nsg"`` field so downstream filters can identify
    these rows by a typed key instead of pattern-matching the
    ``account_name`` sentinel.
    """
    try:
        from api.services.db.ops_audit import record_db_op

        return record_db_op(
            op=op,
            caller=caller,
            account_name="(peering-nsg)",
            db_name=target_nsg_name,
            extra={
                **extra,
                "category": "peering-nsg",
                "destination_ip": destination_ip,
            },
        )
    except Exception:
        LOGGER.exception("audit record_db_op failed for op=%s nsg=%s", op, target_nsg_name)
        return None


def record_audit_event(
    job_id: str | None,
    event: str,
    payload: dict[str, Any],
) -> bool:
    """Record a JobState event. Returns True iff the record actually
    landed (audit backend reachable, job id present).

    The bool return is what lets ``audit_session`` distinguish "backend
    was down so we silently skipped" from "we genuinely recorded the
    terminal event" — critical so the `finally` interrupted-write does
    not pretend success when the underlying append-blob is unreachable.
    """
    if not job_id:
        return False
    try:
        from api.services.db.ops_audit import record_db_op_event

        record_db_op_event(job_id, event, payload)
        return True
    except Exception:
        LOGGER.exception("audit record_db_op_event failed job=%s event=%s", job_id, event)
        return False


@contextmanager
def audit_session(
    *,
    op: str,
    caller: CallerIdentity,
    target_nsg_name: str,
    destination_ip: str,
    extra: dict[str, Any],
) -> Any:
    """Wrap a critical section that mutates ARM with a guaranteed
    terminal-event audit row.

    Records ``op + started`` on entry, yields the ``audit_job_id`` plus
    a closure the caller uses to set the terminal event (``completed``
    / ``failed`` / ``refused`` + payload). The closure mirrors the
    helper's bool return so callers can detect a partial audit drop
    even before the ``finally`` block runs.

    On exit, if the caller never recorded a terminal event — either
    because of an uncaught raise (``handle.release()`` blowing up,
    process death between the ARM call and the finally block) or
    because the audit backend swallowed the terminal call — the
    context manager emits ``interrupted`` so the Audit screen never
    sees a phantom in-flight row. If the audit-start itself failed
    (backend was down at request entry), we still log a WARNING so the
    silent "no audit row at all" outcome is visible in the api log
    instead of being completely invisible.
    """
    audit_job_id = record_audit_started(
        op=op,
        caller=caller,
        target_nsg_name=target_nsg_name,
        destination_ip=destination_ip,
        extra=extra,
    )
    if not audit_job_id:
        LOGGER.warning(
            "settings/vnet-peering audit-start failed (backend unreachable) "
            "op=%s nsg=%s — no audit row will be written for this operator action",
            op,
            target_nsg_name,
        )
    state: dict[str, Any] = {"recorded": False}

    def _set_terminal(event: str, payload: dict[str, Any]) -> bool:
        ok = record_audit_event(audit_job_id, event, payload)
        if ok:
            state["recorded"] = True
        return ok

    try:
        yield audit_job_id, _set_terminal
    finally:
        if not state["recorded"] and audit_job_id:
            # Critique #16: the interrupted-write is the LAST chance to
            # explain the missing terminal row on the Audit screen. If
            # this ALSO fails (audit backend down for the entire
            # request), swallowing the bool means the operator sees a
            # row stuck in ``started`` forever with zero log breadcrumb.
            # Log a WARNING with the job id so an operator chasing the
            # phantom row can grep the api log.
            interrupted_ok = record_audit_event(
                audit_job_id,
                "interrupted",
                {"reason": "no_terminal_event_recorded"},
            )
            if not interrupted_ok:
                LOGGER.error(
                    "settings/vnet-peering audit interrupted-write failed "
                    "audit_job=%s op=%s nsg=%s \u2014 audit row stays in 'started' "
                    "until manual cleanup",
                    audit_job_id,
                    op,
                    target_nsg_name,
                )
