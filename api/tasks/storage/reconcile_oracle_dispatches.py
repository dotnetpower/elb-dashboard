"""Recover accepted order-oracle tasks after broker or worker loss.

Responsibility: Scan a bounded set of active oracle JobState rows and replay
    their existing durable dispatch transaction so unclaimed deliveries,
    published leftovers, and expired execution owners converge.
Edit boundaries: Recovery inventory and delegation only; readiness, claims,
    K8s cleanup, retry state, and broker publication remain in oracle services.
Key entry points: `reconcile_oracle_dispatches`.
Risky contracts: Never scans non-oracle rows, handles at most ten rows per pass,
    never invents scope from environment, and never reactivates an automatic
    delivery while either execution/RBAC gate is off; malformed payloads fail closed.
Validation: `uv run pytest -q api/tests/test_reconcile_oracle_dispatches.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)
_MAX_ROWS = 10
_MAX_SCAN_ROWS = 200
_REQUIRED_FIELDS = (
    "run_id",
    "subscription_id",
    "storage_resource_group",
    "storage_account",
    "cluster_resource_group",
    "cluster_name",
    "db_name",
    "image",
)


@shared_task(
    name="api.tasks.storage.reconcile_oracle_dispatches",
    soft_time_limit=100,
    time_limit=110,
)
def reconcile_oracle_dispatches() -> dict[str, Any]:
    """Re-drive existing durable oracle claims; never create preference work."""
    from api.celery_app import celery_app
    from api.services import get_credential
    from api.services.db.oracle_build import OracleBuildBlocked
    from api.services.db.oracle_dispatch import (
        _recover_terminal_active_claim,
        start_oracle_build,
    )
    from api.services.db.oracle_state import (
        OracleStateConflict,
        oracle_container,
        read_oracle_current,
        read_oracle_run,
    )
    from api.services.state_repo import get_state_repo

    rows = sorted(
        get_state_repo().list_active(job_type="oracle", limit=_MAX_SCAN_ROWS),
        key=lambda row: (
            str(getattr(row, "updated_at", "") or getattr(row, "created_at", "") or ""),
            str(getattr(row, "job_id", "") or ""),
        ),
    )[:_MAX_ROWS]
    result: dict[str, Any] = {
        "status": "completed",
        "scanned": 0,
        "accepted": [],
        "skipped": [],
        "errors": [],
    }
    credential = get_credential()
    for row in rows:
        result["scanned"] += 1
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict) or any(
            not str(payload.get(field) or "") for field in _REQUIRED_FIELDS
        ):
            result["skipped"].append(
                {"job_id": str(getattr(row, "job_id", "") or ""), "reason": "invalid_payload"}
            )
            continue
        automatic = bool(payload.get("automatic"))
        try:
            container = oracle_container(credential, str(payload["storage_account"]))
            recovery = _recover_terminal_active_claim(
                credential,
                container,
                db_name=str(payload["db_name"]),
                now=datetime.now(UTC),
            )
            if recovery == "none":
                current = read_oracle_current(container, str(payload["db_name"]))
                if (
                    isinstance(current, dict)
                    and current.get("status") == "ready"
                    and str(current.get("run_id") or "") == str(payload["run_id"])
                ):
                    try:
                        get_state_repo().update(
                            str(getattr(row, "job_id", "") or ""),
                            status="completed",
                            phase="completed",
                            error_code="",
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "oracle current-only JobState repair failed job_id=%s reason=%s",
                            getattr(row, "job_id", ""),
                            type(exc).__name__,
                        )
                        result["errors"].append(
                            {
                                "job_id": str(getattr(row, "job_id", "") or ""),
                                "error": "jobstate_repair_failed",
                            }
                        )
                        continue
                    result["skipped"].append(
                        {
                            "job_id": str(getattr(row, "job_id", "") or ""),
                            "reason": "recovery_published_no_active",
                        }
                    )
                    continue
                terminal_run = read_oracle_run(
                    container,
                    str(payload["db_name"]),
                    str(payload["run_id"]),
                )
                terminal_status = str((terminal_run or {}).get("status") or "")
                if terminal_status in {"failed", "superseded", "timeout"}:
                    try:
                        get_state_repo().update(
                            str(getattr(row, "job_id", "") or ""),
                            status="failed",
                            phase=terminal_status,
                            error_code=str(
                                (terminal_run or {}).get("error_code") or "oracle_recovered_failure"
                            ),
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "oracle terminal JobState repair failed job_id=%s reason=%s",
                            getattr(row, "job_id", ""),
                            type(exc).__name__,
                        )
                        result["errors"].append(
                            {
                                "job_id": str(getattr(row, "job_id", "") or ""),
                                "error": "jobstate_repair_failed",
                            }
                        )
                        continue
                    result["skipped"].append(
                        {
                            "job_id": str(getattr(row, "job_id", "") or ""),
                            "reason": "recovery_terminal_no_active",
                        }
                    )
                    continue
            if recovery != "active":
                result["skipped"].append(
                    {
                        "job_id": str(getattr(row, "job_id", "") or ""),
                        "reason": f"recovery_{recovery}",
                    }
                )
                continue
            if automatic:
                from api.services.auto_oracle_reconcile import (
                    auto_oracle_execution_enabled,
                )

                if not auto_oracle_execution_enabled():
                    result["skipped"].append(
                        {
                            "job_id": str(getattr(row, "job_id", "") or ""),
                            "reason": "auto_oracle_guard_off",
                        }
                    )
                    continue
            dispatch = start_oracle_build(
                credential,
                subscription_id=str(payload["subscription_id"]),
                storage_resource_group=str(payload["storage_resource_group"]),
                storage_account=str(payload["storage_account"]),
                cluster_resource_group=str(payload["cluster_resource_group"]),
                cluster_name=str(payload["cluster_name"]),
                db_name=str(payload["db_name"]),
                image=str(payload["image"]),
                requested_source_version=str(payload.get("requested_source_version") or ""),
                owner_oid=str(payload.get("requested_by") or ""),
                tenant_id=str(getattr(row, "tenant_id", "") or ""),
                automatic=automatic,
                send_task=celery_app.send_task,
            )
            entry = {
                "job_id": str(getattr(row, "job_id", "") or ""),
                "run_id": dispatch.run_id,
                "status": dispatch.status,
            }
            if dispatch.accepted:
                result["accepted"].append(entry)
            else:
                result["skipped"].append(entry)
        except (OracleBuildBlocked, OracleStateConflict) as exc:
            result["skipped"].append(
                {
                    "job_id": str(getattr(row, "job_id", "") or ""),
                    "reason": getattr(exc, "code", type(exc).__name__),
                }
            )
        except Exception as exc:
            LOGGER.warning(
                "oracle dispatch reconcile failed job_id=%s reason=%s",
                getattr(row, "job_id", ""),
                type(exc).__name__,
            )
            result["errors"].append(
                {
                    "job_id": str(getattr(row, "job_id", "") or ""),
                    "error": type(exc).__name__,
                }
            )
    if result["errors"]:
        result["status"] = "partial"
    return result


__all__ = ["reconcile_oracle_dispatches"]
