"""Activities for storage public-network-access toggling."""

from __future__ import annotations

import logging
from typing import Any

from services import monitoring as monitoring_svc
from services.azure_clients import credential_for_caller

LOGGER = logging.getLogger(__name__)


def activity_set_storage_public_access(payload: dict[str, Any]) -> dict[str, Any]:
    """side-effect: flips storage account `publicNetworkAccess`."""
    cred = credential_for_caller(payload.get("user_assertion"))
    return monitoring_svc.set_storage_public_access(
        cred,
        payload["subscription_id"],
        payload["resource_group"],
        payload["account_name"],
        bool(payload["enabled"]),
    )
