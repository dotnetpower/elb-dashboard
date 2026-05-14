"""Resource provisioning routes (`/api/resources/*`).

Synchronous, idempotent creation of the workspace's foundational resources:
resource group, storage account (HNS-enabled), and ACR. These are wizard
steps that the user performs interactively, so they are kept synchronous
(not Celery tasks) — the SPA blocks until the resource exists, then moves
to the next wizard step.

All Azure SDK calls run under the api sidecar's managed identity, so the
caller does not need to acquire ARM-scoped tokens themselves.

Validation is done at the controller boundary; the legacy
`services.network.ensure_resource_group` / `services.monitoring.ensure_*`
helpers handle the actual idempotent ARM PUT.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api_app.auth import CallerIdentity, require_caller
from api_app.services import get_credential

# Bridge legacy services package
import api_app.services as _bootstrap  # noqa: F401

from services import monitoring as monitoring_svc  # type: ignore  # noqa: E402
from services import network as network_svc  # type: ignore  # noqa: E402
from services.sanitise import sanitise  # type: ignore  # noqa: E402

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resources", tags=["resources"])

_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_STORAGE = re.compile(r"^[a-z0-9]{3,24}$")
_RE_ACR = re.compile(r"^[a-zA-Z0-9]{5,50}$")
_RE_REGION = re.compile(r"^[a-z][a-z0-9]{2,29}$")


def _require_fields(body: dict[str, Any], required: set[str]) -> None:
    missing = required - body.keys()
    if missing:
        raise HTTPException(400, f"missing fields: {sorted(missing)}")


def _check(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or not pattern.match(value):
        raise HTTPException(400, f"invalid {label}: '{sanitise(str(value)[:40])}'")


@router.post("/ensure-rg")
def ensure_rg(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _require_fields(body, {"subscription_id", "resource_group", "region"})
    _check(body["subscription_id"], _RE_SUB, "subscription_id")
    _check(body["resource_group"], _RE_RG, "resource_group")
    _check(body["region"], _RE_REGION, "region")

    cred = get_credential()
    try:
        network_svc.ensure_resource_group(
            cred, body["subscription_id"], body["resource_group"], body["region"],
        )
    except Exception as exc:
        LOGGER.warning("ensure_rg failed: %s", type(exc).__name__)
        raise HTTPException(
            500, f"failed to create resource group: {sanitise(str(exc))[:200]}"
        ) from exc

    LOGGER.info(
        "ensure_rg by oid=%s sub=%s rg=%s region=%s",
        caller.object_id, body["subscription_id"], body["resource_group"], body["region"],
    )
    return {
        "resource_group": body["resource_group"],
        "region": body["region"],
        "status": "created",
    }


@router.post("/ensure-storage")
def ensure_storage(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _require_fields(body, {"subscription_id", "resource_group", "account_name", "region"})
    _check(body["subscription_id"], _RE_SUB, "subscription_id")
    _check(body["resource_group"], _RE_RG, "resource_group")
    _check(body["account_name"], _RE_STORAGE, "account_name")
    _check(body["region"], _RE_REGION, "region")

    cred = get_credential()
    try:
        monitoring_svc.ensure_storage_account(
            cred,
            body["subscription_id"],
            body["resource_group"],
            body["account_name"],
            body["region"],
            caller_oid=caller.object_id,
        )
    except Exception as exc:
        LOGGER.warning("ensure_storage failed: %s", type(exc).__name__)
        raise HTTPException(
            500, f"failed to create storage account: {sanitise(str(exc))[:200]}"
        ) from exc

    LOGGER.info(
        "ensure_storage by oid=%s account=%s region=%s",
        caller.object_id, body["account_name"], body["region"],
    )
    return {
        "account_name": body["account_name"],
        "region": body["region"],
        "status": "created",
    }


@router.post("/ensure-acr")
def ensure_acr(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _require_fields(body, {"subscription_id", "resource_group", "registry_name", "region"})
    _check(body["subscription_id"], _RE_SUB, "subscription_id")
    _check(body["resource_group"], _RE_RG, "resource_group")
    _check(body["registry_name"], _RE_ACR, "registry_name")
    _check(body["region"], _RE_REGION, "region")

    cred = get_credential()
    try:
        monitoring_svc.ensure_acr(
            cred,
            body["subscription_id"],
            body["resource_group"],
            body["registry_name"],
            body["region"],
            caller_oid=caller.object_id,
        )
    except Exception as exc:
        LOGGER.warning("ensure_acr failed: %s", type(exc).__name__)
        raise HTTPException(
            500, f"failed to create ACR: {sanitise(str(exc))[:200]}"
        ) from exc

    LOGGER.info(
        "ensure_acr by oid=%s registry=%s region=%s",
        caller.object_id, body["registry_name"], body["region"],
    )
    return {
        "registry_name": body["registry_name"],
        "region": body["region"],
        "status": "created",
    }
