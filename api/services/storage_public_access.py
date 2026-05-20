"""Local-debug helper: open Storage ``publicNetworkAccess`` to the caller IP.

Responsibility: Local-debug helper: open Storage ``publicNetworkAccess`` to the caller IP
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_truthy`, `is_local_debug_auto_open_enabled`, `is_running_locally`,
`ensure_local_storage_access`, `read_local_storage_state`
Risky contracts: Validate Storage account/blob inputs and preserve the no-browser-SAS policy.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

ENV_OPT_IN = "LOCAL_DEBUG_AUTO_OPEN_STORAGE"
ENV_CONTAINER_APP = "CONTAINER_APP_NAME"
_OFF_HINT = "scripts/dev/storage-public-access.sh off"

# In-process TTL cache for "already open" verdicts. The dashboard polls
# /api/blast/databases every few seconds; without this every poll fired an
# ARM get_properties + an ipify GET, which dominated local CPU.
_CACHE_TTL_SEC = 60.0
_cache_lock = threading.Lock()
_already_open_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_local_debug_auto_open_enabled() -> bool:
    """True only when explicit opt-in is set AND we are not in a Container App."""
    if os.environ.get(ENV_CONTAINER_APP):
        return False
    return _truthy(os.environ.get(ENV_OPT_IN))


def is_running_locally() -> bool:
    """True when the api process is NOT inside a Container App.

    Used by the SPA to decide whether the dashboard should expose the
    "Enable local public access" button. The Container Apps runtime always
    sets ``CONTAINER_APP_NAME``; its absence is the load-bearing signal that
    we are on a developer laptop (or any non-ACA environment).
    """
    return not os.environ.get(ENV_CONTAINER_APP)


def _detect_caller_ip() -> str | None:
    try:
        import httpx

        resp = httpx.get("https://api.ipify.org", timeout=3.0)
        if resp.status_code != 200:
            return None
        ip = resp.text.strip()
        ipaddress.IPv4Address(ip)  # validate; raises if not bare IPv4
    except Exception as exc:
        LOGGER.warning(
            "storage_public_access: caller IP detection failed: %s",
            type(exc).__name__,
        )
        return None
    return ip


def ensure_local_storage_access(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Best-effort: ensure the caller can reach the Storage data plane.

    Idempotent. Never raises. Returns one of:

    * ``{"action": "noop", "reason": ...}`` — gate disabled / inside Container App
    * ``{"action": "already_open", "ip": <caller>, "public": "Enabled", ...}``
    * ``{"action": "ip_added", "ip": <caller>, ...}``
    * ``{"action": "opened", "ip": <caller>, ...}``
    * ``{"action": "failed", "error": "..."}``

    When ``force=True`` the env-var opt-in is bypassed but the
    "not inside a Container App" guard still applies — the deployed control
    plane can never auto-flip Storage open. The dashboard's
    ``POST /api/storage/local-debug/open`` button uses this path so a
    developer can enable access from the UI without exporting the env var.

    Side effect: when ``action`` is ``opened``, the Storage account is
    updated to ``publicNetworkAccess=Enabled``, ``defaultAction=Allow``,
    ``bypass=AzureServices``. No per-IP rules are set — for ADLS Gen2
    (``isHnsEnabled=true``) accounts with an approved private endpoint,
    ``defaultAction=Deny + ipRule`` does not reliably propagate to the data
    plane. ``defaultAction=Allow`` with ``allowSharedKeyAccess=false`` still
    enforces Azure AD authentication on every request. Auto-close is
    intentionally NOT performed — the caller must run
    ``scripts/dev/storage-public-access.sh off``.
    """
    if force:
        # Explicit operator action via the dashboard. The Container-App guard
        # below still protects us; the env var is the only thing being skipped.
        if os.environ.get(ENV_CONTAINER_APP):
            return {
                "action": "noop",
                "reason": "running inside a Container App; refusing to flip public access",
            }
    elif not is_local_debug_auto_open_enabled():
        return {
            "action": "noop",
            "reason": (f"{ENV_OPT_IN} not enabled or running inside a Container App"),
        }

    cache_key = (subscription_id, resource_group, account_name)
    now = time.monotonic()
    with _cache_lock:
        cached = _already_open_cache.get(cache_key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]

    try:
        from api.services.azure_clients import storage_client

        sc = storage_client(credential, subscription_id)
        account = sc.storage_accounts.get_properties(resource_group, account_name)
    except Exception as exc:
        LOGGER.warning(
            "ensure_local_storage_access: ARM read failed for %s: %s",
            account_name,
            type(exc).__name__,
        )
        return {"action": "failed", "error": f"arm_read:{type(exc).__name__}"}

    public_state = str(getattr(account, "public_network_access", "") or "")
    network_rule_set = getattr(account, "network_rule_set", None)
    default_action = (
        str(getattr(network_rule_set, "default_action", "") or "")
        if network_rule_set is not None
        else ""
    )

    # For ADLS Gen2 (isHnsEnabled=true) accounts with an approved private
    # endpoint, defaultAction=Deny + ipRule does not reliably propagate to
    # the data plane even after extended propagation time. defaultAction=Allow
    # + allowSharedKeyAccess=false still enforces Azure AD auth at every
    # request, so this is safe for a local-debug session.
    already_ok = public_state == "Enabled" and default_action == "Allow"
    if already_ok:
        result: dict[str, Any] = {
            "action": "already_open",
            "public": public_state,
            "default_action": default_action,
            "off_hint": _OFF_HINT,
        }
        with _cache_lock:
            _already_open_cache[cache_key] = (now, result)
        return result

    from azure.mgmt.storage.models import (
        NetworkRuleSet,
        StorageAccountUpdateParameters,
    )

    vnet_rules = (
        list(getattr(network_rule_set, "virtual_network_rules", None) or [])
        if network_rule_set is not None
        else []
    )
    new_rules = NetworkRuleSet(
        bypass="AzureServices",
        default_action="Allow",
        virtual_network_rules=vnet_rules,
        # No ip_rules: defaultAction=Allow makes per-IP rules redundant.
        # Deny+ipRule does not work reliably for ADLS Gen2+private endpoint.
    )
    update = StorageAccountUpdateParameters(
        public_network_access="Enabled",
        network_rule_set=new_rules,
    )

    try:
        sc.storage_accounts.update(resource_group, account_name, update)
    except Exception as exc:
        LOGGER.warning(
            "ensure_local_storage_access: ARM update failed for %s: %s",
            account_name,
            type(exc).__name__,
        )
        return {"action": "failed", "error": f"arm_update:{type(exc).__name__}"}

    caller_ip = _detect_caller_ip()  # informational only
    LOGGER.warning(
        "ensure_local_storage_access: opened account=%s defaultAction=Allow previous_public=%s "
        "(LOCAL_DEBUG_AUTO_OPEN_STORAGE active — remember to run `%s`)",
        account_name,
        public_state or "Disabled",
        _OFF_HINT,
    )
    result = {
        "action": "opened",
        "previous_public": public_state or "Disabled",
        "default_action": "Allow",
        "off_hint": _OFF_HINT,
    }
    if caller_ip:
        result["ip"] = caller_ip
    with _cache_lock:
        _already_open_cache[cache_key] = (now, result)
    return result


def read_local_storage_state(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> dict[str, Any]:
    """Read-only inspection used by ``GET /api/storage/local-debug``.

    Returns ``{is_local, public_access, default_action, ip_rules,
    caller_ip}`` so the SPA can render a "Storage public access disabled —
    Enable for local debugging" affordance only when running on a developer
    laptop. Never writes.
    """
    state: dict[str, Any] = {
        "is_local": is_running_locally(),
        "public_access": None,
        "default_action": None,
        "ip_rules": [],
        "caller_ip": None,
        "caller_ip_in_rules": False,
    }
    if not state["is_local"]:
        return state

    state["caller_ip"] = _detect_caller_ip()
    try:
        from api.services.azure_clients import storage_client

        sc = storage_client(credential, subscription_id)
        account = sc.storage_accounts.get_properties(resource_group, account_name)
    except Exception as exc:
        LOGGER.debug(
            "read_local_storage_state: ARM read failed for %s: %s",
            account_name,
            type(exc).__name__,
        )
        state["error"] = f"arm_read:{type(exc).__name__}"
        return state

    public_state = str(getattr(account, "public_network_access", "") or "")
    network_rule_set = getattr(account, "network_rule_set", None)
    default_action = (
        str(getattr(network_rule_set, "default_action", "") or "")
        if network_rule_set is not None
        else ""
    )
    ip_rules: list[str] = []
    if network_rule_set is not None:
        for rule in getattr(network_rule_set, "ip_rules", None) or []:
            value = getattr(rule, "ip_address_or_range", None) or getattr(rule, "value", None)
            if value:
                ip_rules.append(str(value))

    state["public_access"] = public_state or "Disabled"
    state["default_action"] = default_action or None
    state["ip_rules"] = ip_rules
    state["caller_ip_in_rules"] = state["caller_ip"] is not None and state["caller_ip"] in ip_rules
    return state
