"""Private VNet peering and probe settings route.

Responsibility: Peer a target VNet into the selected AKS cluster VNet and
probe the private OpenAPI endpoint so the operator can verify reachability
from the remote network.
Edit boundaries: HTTP validation + response shaping only. All Azure SDK work
is delegated to `api.tasks.azure.peering`.
Key entry points: `peer_vnet`.
Risky contracts: The helper already absorbs per-peering failures into the
returned payload. This route only turns hard helper failures into a stable
502 so the SPA can show a recoverable error instead of a raw 500.
Validation: `uv run pytest -q api/tests/test_settings_vnet_peering.py`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$")


def _require(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = (value or "").strip()
    if not isinstance(value, str) or not pattern.match(text):
        raise HTTPException(400, f"invalid {label}")
    return text


@router.post("/vnet-peering")
def peer_vnet(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group = _require(body.get("resource_group"), _RE_RG, "resource_group")
    cluster_name = _require(body.get("cluster_name"), _RE_NAME, "cluster_name")
    target_subscription_id = _require(
        body.get("target_subscription_id"), _RE_SUB, "target_subscription_id"
    )
    target_resource_group = _require(
        body.get("target_resource_group"), _RE_RG, "target_resource_group"
    )
    target_vnet_name = _require(
        body.get("target_vnet_name"), _RE_NAME, "target_vnet_name"
    )

    target_ip = str(body.get("target_ip") or "10.224.0.7").strip()
    try:
        ipaddress.ip_address(target_ip)
    except ValueError as exc:
        raise HTTPException(400, "invalid target_ip") from exc

    target_path = str(body.get("target_path") or "/openapi.json").strip()
    if not target_path.startswith("/"):
        target_path = f"/{target_path}"

    LOGGER.info(
        "settings/vnet-peering requested cluster=%s rg=%s target=%s/%s caller_oid=%s",
        cluster_name,
        resource_group,
        target_resource_group,
        target_vnet_name,
        caller.object_id,
    )

    from api.services import get_credential
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    try:
        summary = ensure_vnet_peering_with_target(
            get_credential(),
            subscription_id=subscription_id,
            cluster_resource_group=resource_group,
            cluster_name=cluster_name,
            target_subscription_id=target_subscription_id,
            target_resource_group=target_resource_group,
            target_vnet_name=target_vnet_name,
            target_ip=target_ip,
            target_path=target_path,
        )
    except Exception as exc:
        LOGGER.exception("settings/vnet-peering helper failed")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "vnet_peering_unavailable",
                "message": (
                    "VNet peering could not be evaluated: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            },
        ) from exc

    return summary
