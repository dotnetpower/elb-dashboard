"""Reconcile Auto oracle preferences into idempotent build dispatches.

Responsibility: Select a bounded fair slice of enabled preferences, fail-closed
    revalidate owner RBAC and durable retry state, and invoke the shared oracle
    dispatch for eligible databases.
Edit boundaries: Preference-level reconciliation only; preference persistence,
    build readiness/claim/task execution, HTTP routes, and retention live in
    focused modules.
Key entry points: `reconcile_auto_oracle_preferences`,
    `auto_oracle_owner_authorized`, `auto_oracle_dependency_ready`,
    `enqueue_targeted_auto_oracle`.
Risky contracts: Feature defaults OFF; owner RBAC lookup degradation blocks;
    expected readiness blockers consume no retry budget; at most two builds are
    accepted per tick and at most 50 preferences are inspected.
Validation: `uv run pytest -q api/tests/test_auto_oracle_reconcile.py`.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from api.auth import DEV_BYPASS_OID
from api.services.auto_oracle import (
    AutoOraclePreference,
    get_auto_oracle_preference,
    get_auto_oracle_scan_cursor,
    list_auto_oracle_preference_page,
    save_auto_oracle_scan_cursor,
)
from api.services.db.oracle_build import OracleBuildBlocked
from api.services.db.oracle_dispatch import start_oracle_build
from api.services.db.oracle_retry import automation_retry_allowed
from api.services.db.oracle_state import (
    OracleBuildInProgress,
    OracleStateConflict,
    oracle_container,
    read_oracle_automation,
    update_oracle_automation,
)
from api.services.env import env_int
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_MAX_ENQUEUES = env_int("AUTO_ORACLE_MAX_ENQUEUES_PER_TICK", 2, minimum=1, maximum=10)
_MAX_ENQUEUES_PER_STORAGE = env_int("AUTO_ORACLE_MAX_ENQUEUES_PER_STORAGE", 1, minimum=1, maximum=5)
_MAX_INSPECTIONS = env_int("AUTO_ORACLE_MAX_INSPECTIONS_PER_TICK", 50, minimum=1, maximum=500)

SendTask = Callable[..., Any]


def auto_oracle_execution_enabled() -> bool:
    requested = os.environ.get("AUTO_ORACLE_RECONCILE_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if requested and not auto_oracle_rbac_enforced():
        LOGGER.warning("Auto oracle reconcile remains disabled until ENFORCE_AUTO_ORACLE_RBAC=true")
        return False
    return requested


def auto_oracle_rbac_enforced() -> bool:
    return os.environ.get("ENFORCE_AUTO_ORACLE_RBAC", "false").lower() in {
        "1",
        "true",
        "yes",
    }


def enqueue_targeted_auto_oracle(
    preference: AutoOraclePreference,
    *,
    send_task: SendTask,
) -> str:
    """Best-effort low-latency trigger; periodic beat remains the recovery path."""
    if not preference.enabled or not auto_oracle_execution_enabled():
        return ""
    try:
        task = send_task(
            "api.tasks.storage.reconcile_auto_oracle",
            kwargs={"preference": preference.to_dict()},
            queue="reconcile",
        )
        return str(getattr(task, "id", "") or "queued")
    except Exception as exc:
        LOGGER.warning(
            "targeted auto oracle enqueue failed db=%s reason=%s",
            preference.db_name,
            type(exc).__name__,
        )
        return ""


def auto_oracle_owner_authorized(
    credential: Any,
    preference: AutoOraclePreference,
) -> tuple[bool, str]:
    """Fail-closed current owner capability check for background mutation."""
    return oracle_caller_write_authorized(
        credential,
        caller_oid=preference.owner_oid,
        subscription_id=preference.subscription_id,
        cluster_resource_group=preference.cluster_resource_group,
        cluster_name=preference.cluster_name,
        storage_resource_group=preference.storage_resource_group,
    )


def oracle_caller_write_authorized(
    credential: Any,
    *,
    caller_oid: str,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    storage_resource_group: str,
) -> tuple[bool, str]:
    """Fail-closed AKS and Storage write check for oracle mutations."""
    if not auto_oracle_rbac_enforced():
        return True, "legacy_guard_off"
    if not os.environ.get("CONTAINER_APP_NAME") and caller_oid == DEV_BYPASS_OID:
        return True, "dev_bypass"
    if not caller_oid:
        return False, "owner_missing"
    from api.services.me_permissions import compute_caller_permissions

    cluster = compute_caller_permissions(
        credential,
        caller_oid=caller_oid,
        subscription_id=subscription_id,
        resource_group=cluster_resource_group,
        cluster_name=cluster_name,
    )
    if cluster.degraded:
        return False, "cluster_permission_indeterminate"
    if not cluster.can_write:
        return False, "cluster_write_denied"
    storage = compute_caller_permissions(
        credential,
        caller_oid=caller_oid,
        subscription_id=subscription_id,
        resource_group=storage_resource_group,
    )
    if storage.degraded:
        return False, "storage_permission_indeterminate"
    if not storage.can_write:
        return False, "storage_write_denied"
    return True, "authorized"


def auto_oracle_scope_read_authorized(
    credential: Any,
    *,
    caller_oid: str,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
) -> tuple[bool, str]:
    """Fail-closed read authorization for the shared preference view."""
    if not auto_oracle_rbac_enforced():
        return True, "legacy_guard_off"
    if not os.environ.get("CONTAINER_APP_NAME") and caller_oid == DEV_BYPASS_OID:
        return True, "dev_bypass"
    if not caller_oid:
        return False, "caller_missing"
    from api.services.me_permissions import compute_caller_permissions

    permissions = compute_caller_permissions(
        credential,
        caller_oid=caller_oid,
        subscription_id=subscription_id,
        resource_group=cluster_resource_group,
        cluster_name=cluster_name,
    )
    if permissions.degraded:
        return False, "cluster_permission_indeterminate"
    if not permissions.can_read:
        return False, "cluster_read_denied"
    return True, "authorized"


def auto_oracle_dependency_ready(
    preference: AutoOraclePreference,
) -> tuple[bool, str]:
    """Require the exact cluster/Storage/DB to remain opted into Auto warm."""
    from api.services.auto_warmup import get_auto_warmup_preference

    warm = get_auto_warmup_preference(
        preference.subscription_id,
        preference.cluster_resource_group,
        preference.cluster_name,
    )
    if warm is None or not warm.enabled:
        return False, "auto_warm_disabled"
    if preference.db_name not in warm.databases:
        return False, "auto_warm_db_disabled"
    if warm.storage_account.lower() != preference.storage_account.lower():
        return False, "auto_warm_storage_mismatch"
    if warm.storage_resource_group.lower() != preference.storage_resource_group.lower():
        return False, "auto_warm_storage_rg_mismatch"
    return True, "ready"


def _mark_blocked(
    container: Any,
    *,
    db_name: str,
    reason: str,
    current_state: dict[str, Any] | None = None,
) -> None:
    if (
        isinstance(current_state, dict)
        and current_state.get("status") == "blocked"
        and current_state.get("blocked_reason") == reason
    ):
        return
    try:
        update_oracle_automation(
            container,
            db_name=db_name,
            updates={
                "status": "blocked",
                "blocked_reason": reason,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        from api.services.feature_events import record_feature_event

        record_feature_event(
            "oracle_automation_blocked",
            status="info",
            database=db_name,
            reason=reason,
        )
    except Exception as exc:
        LOGGER.debug(
            "auto oracle blocked state skipped db=%s reason=%s",
            db_name,
            type(exc).__name__,
        )


def reconcile_auto_oracle_preferences(
    *,
    credential: Any,
    send_task: SendTask,
    preference: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Inspect preferences and enqueue a bounded number of oracle builds."""
    if enabled is None:
        enabled = auto_oracle_execution_enabled()
    if not enabled:
        return {"status": "disabled", "inspected": 0, "enqueued": [], "skipped": []}

    targeted = isinstance(preference, dict)
    next_cursor = ""
    cursor_reset = False
    if isinstance(preference, dict):
        targeted_preference = AutoOraclePreference.from_dict(preference)
        selected = [targeted_preference] if targeted_preference.enabled else []
    else:
        cursor = get_auto_oracle_scan_cursor("reconcile")
        try:
            selected, next_cursor = list_auto_oracle_preference_page(
                limit=_MAX_INSPECTIONS,
                continuation_token=cursor,
                enabled_only=True,
            )
        except Exception as exc:
            if not cursor:
                raise
            LOGGER.warning(
                "auto oracle reconcile cursor invalid; restarting scan reason=%s",
                type(exc).__name__,
            )
            cursor_reset = True
            selected, next_cursor = list_auto_oracle_preference_page(
                limit=_MAX_INSPECTIONS,
                continuation_token="",
                enabled_only=True,
            )
    result: dict[str, Any] = {
        "status": "completed",
        "inspected": 0,
        "enqueued": [],
        "skipped": [],
        "errors": [],
        "cursor_reset": cursor_reset,
    }
    enqueued_by_storage: dict[str, int] = {}
    for pref in selected:
        if len(result["enqueued"]) >= _MAX_ENQUEUES:
            break
        result["inspected"] += 1
        if enqueued_by_storage.get(pref.storage_account, 0) >= _MAX_ENQUEUES_PER_STORAGE:
            result["skipped"].append({"db": pref.db_name, "reason": "storage_enqueue_cap"})
            continue
        try:
            current_preference = get_auto_oracle_preference(
                pref.subscription_id,
                pref.cluster_resource_group,
                pref.cluster_name,
                pref.storage_account,
                pref.db_name,
            )
            if current_preference is None or not current_preference.enabled:
                result["skipped"].append({"db": pref.db_name, "reason": "preference_disabled"})
                continue
            pref = current_preference
            container = oracle_container(credential, pref.storage_account)
            retry_state = read_oracle_automation(container, pref.db_name)
            retry_allowed, retry_reason = automation_retry_allowed(retry_state)
            if not retry_allowed:
                result["skipped"].append({"db": pref.db_name, "reason": retry_reason})
                continue
            dependency_ready, dependency_reason = auto_oracle_dependency_ready(pref)
            if not dependency_ready:
                _mark_blocked(
                    container,
                    db_name=pref.db_name,
                    reason=dependency_reason,
                    current_state=retry_state,
                )
                result["skipped"].append({"db": pref.db_name, "reason": dependency_reason})
                continue
            authorized, auth_reason = auto_oracle_owner_authorized(credential, pref)
            if not authorized:
                _mark_blocked(
                    container,
                    db_name=pref.db_name,
                    reason=auth_reason,
                    current_state=retry_state,
                )
                result["skipped"].append({"db": pref.db_name, "reason": auth_reason})
                continue
            image = pref.image.strip()
            if not image and pref.acr_name:
                from api.services.image_tags import IMAGE_TAGS

                image = (
                    f"{pref.acr_name.strip().lower()}.azurecr.io/ncbi/elb:{IMAGE_TAGS['ncbi/elb']}"
                )
            dispatch = start_oracle_build(
                credential,
                subscription_id=pref.subscription_id,
                storage_resource_group=pref.storage_resource_group,
                storage_account=pref.storage_account,
                cluster_resource_group=pref.cluster_resource_group,
                cluster_name=pref.cluster_name,
                db_name=pref.db_name,
                image=image,
                owner_oid=pref.owner_oid,
                tenant_id=pref.tenant_id,
                automatic=True,
                send_task=send_task,
            )
            if dispatch.accepted:
                enqueued_by_storage[pref.storage_account] = (
                    enqueued_by_storage.get(pref.storage_account, 0) + 1
                )
                result["enqueued"].append(
                    {
                        "db": pref.db_name,
                        "run_id": dispatch.run_id,
                        "task_id": dispatch.task_id,
                    }
                )
            else:
                result["skipped"].append({"db": pref.db_name, "reason": dispatch.status})
        except OracleBuildBlocked as exc:
            _mark_blocked(
                container,
                db_name=pref.db_name,
                reason=exc.code,
                current_state=retry_state,
            )
            result["skipped"].append({"db": pref.db_name, "reason": exc.code})
        except OracleBuildInProgress:
            result["skipped"].append({"db": pref.db_name, "reason": "build_in_progress"})
        except OracleStateConflict as exc:
            result["errors"].append({"db": pref.db_name, "error": type(exc).__name__})
        except Exception as exc:
            LOGGER.warning(
                "auto oracle reconcile failed db=%s reason=%s",
                pref.db_name,
                type(exc).__name__,
            )
            result["errors"].append(
                {
                    "db": pref.db_name,
                    "error": type(exc).__name__,
                    "message": sanitise(str(exc))[:200],
                }
            )
    if not targeted:
        try:
            save_auto_oracle_scan_cursor("reconcile", next_cursor)
        except Exception as exc:
            LOGGER.warning(
                "auto oracle reconcile cursor write failed reason=%s",
                type(exc).__name__,
            )
            result["errors"].append({"error": "reconcile_cursor_write_failed"})
    if result["errors"]:
        result["status"] = "partial"
    return result


__all__ = [
    "auto_oracle_dependency_ready",
    "auto_oracle_execution_enabled",
    "auto_oracle_owner_authorized",
    "auto_oracle_rbac_enforced",
    "auto_oracle_scope_read_authorized",
    "enqueue_targeted_auto_oracle",
    "oracle_caller_write_authorized",
    "reconcile_auto_oracle_preferences",
]
