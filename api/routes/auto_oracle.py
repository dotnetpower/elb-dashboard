"""Authenticated Auto oracle preference API.

Responsibility: Validate GET/PUT preference requests, enforce that an enabled
    DB is already configured for Auto warm on the same cluster, stamp caller
    ownership, verify current write capabilities, enqueue reconciliation, and
    shape preference responses.
Edit boundaries: HTTP/auth/response shaping only; persistence is in
    `api.services.auto_oracle` and build reconciliation is task-backed.
Key entry points: `auto_oracle_preference_put`,
    `auto_oracle_preferences_get`.
Risky contracts: Every route enforces `require_caller`; enabling without an
    Auto warm dependency or current AKS/Storage write permission is rejected;
    every preference mutation requires current write permission; caller IDs
    never leave the API; cloud mutation remains task-backed.
Validation: `uv run pytest -q api/tests/test_auto_oracle_route.py`.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.auth import CallerIdentity, require_caller
from api.services.auto_oracle import AutoOraclePreference
from api.services.sanitise import sanitise

router = APIRouter(prefix="/api/warmup", tags=["warmup"])
LOGGER = logging.getLogger(__name__)
_SUBSCRIPTION_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.()-]{1,120}$")
_STORAGE_RE = re.compile(r"^[a-z0-9]{3,24}$")


def _public_preference(pref: AutoOraclePreference) -> dict[str, object]:
    value = dict(pref.to_dict())
    value.pop("owner_oid", None)
    value.pop("tenant_id", None)
    value["version"] = pref.etag
    return value


class AutoOraclePreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    cluster_resource_group: str
    cluster_name: str
    storage_resource_group: str
    storage_account: str
    db_name: str
    acr_name: str | None = None
    image: str | None = None
    enabled: bool = False
    reset_retry: bool = False
    version: str | None = Field(default=None, max_length=1024)


@router.put("/oracle-preference")
def auto_oracle_preference_put(
    body: AutoOraclePreferenceRequest,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    from api.services.auto_oracle import (
        get_auto_oracle_preference,
        normalise_auto_oracle_preference,
        save_auto_oracle_preference,
    )
    from api.services.preference_concurrency import PreferenceUpdateConflict

    value = body.model_dump(exclude={"reset_retry"})
    value.update({"owner_oid": caller.object_id, "tenant_id": caller.tenant_id})
    try:
        pref = normalise_auto_oracle_preference(value)
    except ValueError as exc:
        raise HTTPException(
            400,
            {
                "code": "invalid_auto_oracle_preference",
                "message": sanitise(str(exc))[:200],
            },
        ) from exc
    from api.services import get_credential
    from api.services.auto_oracle_reconcile import (
        auto_oracle_execution_enabled,
        auto_oracle_owner_authorized,
        auto_oracle_rbac_enforced,
    )

    authorized, reason = auto_oracle_owner_authorized(get_credential(), pref)
    if not authorized:
        LOGGER.warning(
            "auto oracle preference write denied reason=%s",
            reason,
        )
        raise HTTPException(
            403,
            {
                "code": "auto_oracle_permission_denied",
                "message": "AKS and Storage write permissions are required for Auto oracle.",
            },
        )
    if pref.enabled:
        from api.services.auto_oracle_reconcile import (
            auto_oracle_dependency_ready,
        )

        dependency_ready, _dependency_reason = auto_oracle_dependency_ready(pref)
        if not dependency_ready:
            raise HTTPException(
                409,
                {
                    "code": "auto_warm_required",
                    "message": "Enable Auto warm for this database before Auto oracle.",
                },
            )
    create_only = False
    modifier_changed = False
    if auto_oracle_rbac_enforced():
        existing = get_auto_oracle_preference(
            pref.subscription_id,
            pref.cluster_resource_group,
            pref.cluster_name,
            pref.storage_account,
            pref.db_name,
        )
        submitted_version = (body.version or "").strip()
        if existing is not None:
            # This is a shared resource preference, not a per-user object.
            # Any current AKS+Storage writer may update it with a fresh version;
            # owner_oid becomes the latest modifier whose RBAC is rechecked by
            # background execution. ETag CAS prevents concurrent takeover.
            modifier_changed = bool(existing.owner_oid and existing.owner_oid != caller.object_id)
            if not submitted_version or submitted_version != existing.etag:
                raise HTTPException(
                    409,
                    {
                        "code": "auto_oracle_preference_conflict",
                        "message": "Auto oracle settings changed; refresh and retry.",
                    },
                )
        elif submitted_version:
            raise HTTPException(
                409,
                {
                    "code": "auto_oracle_preference_conflict",
                    "message": "Auto oracle settings changed; refresh and retry.",
                },
            )
        else:
            create_only = True
    try:
        saved = save_auto_oracle_preference(pref, create_only=create_only)
    except PreferenceUpdateConflict as exc:
        raise HTTPException(
            409,
            {
                "code": "auto_oracle_preference_conflict",
                "message": "Auto oracle settings changed; refresh and retry.",
            },
        ) from exc
    from api.services.feature_events import record_feature_event

    record_feature_event(
        "oracle_preference_saved",
        status="info",
        database=saved.db_name,
        enabled=saved.enabled,
        modifier_changed=modifier_changed,
    )
    if saved.enabled and body.reset_retry:
        from api.services import get_credential
        from api.services.db.oracle_retry import reset_automation_retry
        from api.services.db.oracle_state import oracle_container

        try:
            reset_result = reset_automation_retry(
                oracle_container(get_credential(), saved.storage_account),
                db_name=saved.db_name,
            )
            if (
                bool(reset_result.get("retry_exhausted"))
                or int(reset_result.get("failure_count") or 0) != 0
            ):
                raise RuntimeError("retry state did not reset")
        except Exception as exc:
            raise HTTPException(
                503,
                {
                    "code": "auto_oracle_retry_reset_failed",
                    "message": "Could not reset the Auto oracle retry state.",
                },
            ) from exc
    reconcile_task_id = ""
    if saved.enabled:
        from api.celery_app import celery_app
        from api.services.auto_oracle_reconcile import (
            enqueue_targeted_auto_oracle,
        )

        reconcile_task_id = enqueue_targeted_auto_oracle(saved, send_task=celery_app.send_task)
    response_status = "saved"
    if saved.enabled and not reconcile_task_id:
        response_status = (
            "saved_no_immediate_enqueue" if auto_oracle_execution_enabled() else "saved_inactive"
        )
    return {
        "status": response_status,
        "preference": _public_preference(saved),
        "reconcile_task_id": reconcile_task_id,
        "modifier_changed": modifier_changed,
    }


@router.get("/oracle-preferences")
def auto_oracle_preferences_get(
    subscription_id: str = Query(...),
    cluster_resource_group: str = Query(...),
    cluster_name: str = Query(...),
    storage_account: str = Query(...),
    cursor: str = Query("", max_length=8192),
    limit: int = Query(200, ge=1, le=500),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    if (
        not _SUBSCRIPTION_RE.fullmatch(subscription_id)
        or not _SEGMENT_RE.fullmatch(cluster_resource_group)
        or not _SEGMENT_RE.fullmatch(cluster_name)
        or not _STORAGE_RE.fullmatch(storage_account)
    ):
        raise HTTPException(
            400,
            {
                "code": "invalid_auto_oracle_scope",
                "message": "A valid subscription, cluster, and Storage account are required.",
            },
        )
    from api.services import get_credential
    from api.services.auto_oracle import list_auto_oracle_preference_page
    from api.services.auto_oracle_reconcile import (
        auto_oracle_scope_read_authorized,
    )

    authorized, reason = auto_oracle_scope_read_authorized(
        get_credential(),
        caller_oid=caller.object_id,
        subscription_id=subscription_id,
        cluster_resource_group=cluster_resource_group,
        cluster_name=cluster_name,
    )
    if not authorized:
        LOGGER.warning("auto oracle preference read denied reason=%s", reason)
        raise HTTPException(
            403,
            {
                "code": "auto_oracle_read_denied",
                "message": "Read access to the AKS cluster is required.",
            },
        )

    try:
        preferences, next_cursor = list_auto_oracle_preference_page(
            limit=limit,
            continuation_token=cursor,
            subscription_id=subscription_id,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
            storage_account=storage_account,
        )
    except ValueError as exc:
        raise HTTPException(
            400,
            {
                "code": "invalid_auto_oracle_cursor",
                "message": "The Auto oracle page cursor is invalid.",
            },
        ) from exc
    return {
        "preferences": [_public_preference(pref) for pref in preferences],
        "next_cursor": next_cursor,
    }


__all__ = ["AutoOraclePreferenceRequest", "router"]
