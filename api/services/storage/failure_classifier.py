"""Storage data-plane exception classifier.

Responsibility: Convert Storage SDK/ARM exceptions into dashboard-friendly
`degraded` payloads that distinguish network posture from RBAC/not-found cases.
Edit boundaries: Classification only. Do not perform blob reads/writes here.
Key entry points: `classify_storage_failure`.
Risky contracts: Never return SAS URLs or browser-fetchable Storage URLs. Local
public access guidance must remain IP-allowlist only.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)


def classify_storage_failure(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Classify a Storage data-plane exception into a UI-friendly degraded shape.

    Azure Storage returns the same ``AuthorizationFailure`` error code for two
    very different conditions:

    * **Network deny** — ``publicNetworkAccess: Disabled`` (or ``networkAcls``
      explicitly denies the caller) and the request did not arrive from a
      private endpoint. This is the steady-state for this project (see
      ``.github/copilot-instructions.md`` §9) and is **expected** when running
      the api sidecar from a developer laptop.
    * **RBAC deny** — the storage data plane is reachable but the caller lacks
      the ``Storage Blob Data *`` role at the account / container scope.

    To distinguish the two we look at the account's ``publicNetworkAccess``
    via ARM (management plane, which is reachable from anywhere with the right
    role). The result is the dict shape consumed by ``/api/blast/*`` routes.
    """
    err_str = str(exc)
    err_type = type(exc).__name__

    # The Azure SDK reports "missing blob/container/account" as one of three
    # concrete exception types. The class name is the reliable signal: the
    # rendered ``str(exc)`` is a free-form HTTP response body that may carry
    # ``ErrorCode:BlobNotFound`` (no "ResourceNotFound" substring), so the old
    # substring-only check missed the common case and the route then returned
    # 503 for a plain "this blob does not exist" — surfacing to the SPA as
    # ``storage_unreachable`` rather than the truthful 404. Keep the substring
    # check as a secondary signal for SDK versions that surface a generic
    # exception with the code in the message.
    if (
        err_type in {"ResourceNotFoundError", "BlobNotFoundError", "ContainerNotFoundError"}
        or "ResourceNotFound" in err_str
        or "BlobNotFound" in err_str
        or "AccountNotFound" in err_str
        or "ContainerNotFound" in err_str
    ):
        suffix = f" in resource group '{resource_group}'." if resource_group else "."
        return {
            "degraded": True,
            "degraded_reason": "not_found",
            "message": f"Storage container or account '{account_name}' not found{suffix}",
        }

    is_permission_mismatch = "AuthorizationPermissionMismatch" in err_str
    is_authorization_failure = "AuthorizationFailure" in err_str
    is_auth_like = (
        is_authorization_failure
        or is_permission_mismatch
        or "This request is not authorized" in err_str
    )
    if not is_auth_like:
        return {
            "degraded": True,
            "degraded_reason": err_type,
            "message": f"Storage call failed: {err_type}",
        }

    public_state: str | None = None
    default_action: str | None = None
    ip_rules: list[str] = []
    if subscription_id and resource_group:
        try:
            from api.services.azure_clients import storage_client

            sc = storage_client(credential, subscription_id)
            account = sc.storage_accounts.get_properties(resource_group, account_name)
            raw = getattr(account, "public_network_access", None)
            public_state = str(raw) if raw is not None else None
            network_rule_set = getattr(account, "network_rule_set", None)
            if network_rule_set is not None:
                raw_default = getattr(network_rule_set, "default_action", None)
                default_action = str(raw_default) if raw_default is not None else None
                for rule in getattr(network_rule_set, "ip_rules", None) or []:
                    ip_value = getattr(rule, "ip_address_or_range", None) or getattr(
                        rule, "value", None
                    )
                    if ip_value:
                        ip_rules.append(str(ip_value))
        except Exception as arm_exc:
            LOGGER.debug("classify_storage_failure ARM check failed: %s", arm_exc)

    if public_state == "Disabled":
        return {
            "degraded": True,
            "degraded_reason": "network_blocked",
            "public_access_disabled": True,
            "message": (
                f"Storage account '{account_name}' is Private only "
                "(publicNetworkAccess: Disabled; production posture — see project policy §9). "
                "Data-plane access only works from inside the platform VNet via the "
                "private endpoint, so this view is unavailable from local development. "
                "To debug locally, open an "
                "IP-allowlisted window with "
                f"`scripts/dev/storage-public-access.sh on --account {account_name} "
                f"--rg {resource_group or '<resource-group>'}` and close it again with "
                "`storage-public-access.sh off` when done. In a deployed environment, "
                "run `azd up` and verify from the Container App."
            ),
        }

    if is_authorization_failure and public_state == "Enabled" and default_action == "Deny":
        caller_ip: str | None = None
        try:
            from api.services.storage.public_access import _detect_caller_ip

            caller_ip = _detect_caller_ip()
        except Exception as ip_exc:
            LOGGER.debug("classify_storage_failure caller IP check failed: %s", ip_exc)
        caller_ip_in_rules = bool(caller_ip and caller_ip in ip_rules)
        local_detail = (
            f" Current detected IP {caller_ip} is already in ipRules; Azure may still be "
            "propagating the firewall update or seeing a different egress path."
            if caller_ip_in_rules
            else f" Current detected IP {caller_ip or '<unknown>'} is not in ipRules."
        )
        return {
            "degraded": True,
            "degraded_reason": "firewall_blocked",
            "public_access_disabled": False,
            "local_debug_access_blocked": True,
            "caller_ip": caller_ip,
            "caller_ip_in_rules": caller_ip_in_rules,
            "message": (
                f"Storage account '{account_name}' allows only selected networks "
                "(publicNetworkAccess: Enabled, defaultAction: Deny), and Storage still "
                "rejected this local data-plane request. "
                f"Run `scripts/dev/storage-public-access.sh on --account {account_name} "
                f"--rg {resource_group or '<resource-group>'}` to refresh the IP allowlist, "
                "then retry after firewall propagation."
                f"{local_detail} If this persists after the network check passes, refresh "
                "your Azure CLI login so data-plane RBAC tokens are current."
            ),
        }

    if os.environ.get("CONTAINER_APP_NAME") or os.environ.get("IDENTITY_ENDPOINT"):
        remediation = (
            "Assign 'Storage Blob Data Reader' (or Contributor for write) on the "
            "storage account to the shared managed identity attached to this "
            "Container App, then wait a few minutes for RBAC propagation."
        )
    else:
        remediation = (
            "Assign 'Storage Blob Data Reader' (or Contributor for write) on the "
            "storage account to your az login identity, then wait a few minutes "
            "for RBAC propagation."
        )

    return {
        "degraded": True,
        "degraded_reason": "access_denied",
        "message": (
            f"Cannot read data from storage account '{account_name}'. "
            f"{remediation}"
        ),
    }
