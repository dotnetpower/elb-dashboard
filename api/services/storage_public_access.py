"""Local-debug helper: open Storage ``publicNetworkAccess`` to the caller IP.

In-process equivalent of ``scripts/dev/storage-public-access.sh on``, gated to
local development only. See ``.github/copilot-instructions.md`` §9.

Why this exists
---------------
Production keeps every workload Storage account ``publicNetworkAccess:
Disabled`` and reaches the data plane via a private endpoint inside the
platform VNet. A developer running the api sidecar from a laptop has no such
reachability and gets connection / ``AuthorizationFailure`` errors for every
blob call, including the server-side copy used by ``/api/storage/prepare-db``.

This helper performs the same flip the shell script does — set
``publicNetworkAccess=Enabled``, ``defaultAction=Deny``, append the caller's
public IP to ``ipRules`` — but only when **both**:

* env ``LOCAL_DEBUG_AUTO_OPEN_STORAGE`` is truthy, **and**
* the process is NOT running inside a Container App (no
  ``CONTAINER_APP_NAME`` in env). This is the load-bearing operational
  guard — it MUST stay so the deployed control plane can never auto-flip
  Storage open.

It never auto-closes. The caller is expected to run
``scripts/dev/storage-public-access.sh off`` when done; ``prepare_db``
forwards the reminder in its response message.
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

    Side effect: when ``action`` is ``opened`` or ``ip_added``, the Storage
    account is updated to ``publicNetworkAccess=Enabled``,
    ``defaultAction=Deny``, ``bypass=AzureServices`` and ``ipRules`` extended
    with the caller's public IPv4. Auto-close is intentionally NOT performed —
    the caller must run ``scripts/dev/storage-public-access.sh off``.
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
    existing_ips: list[str] = []
    if network_rule_set is not None:
        for rule in getattr(network_rule_set, "ip_rules", None) or []:
            ip_value = getattr(rule, "ip_address_or_range", None) or getattr(rule, "value", None)
            if ip_value:
                existing_ips.append(str(ip_value))

    caller_ip = _detect_caller_ip()
    if caller_ip is None:
        return {"action": "failed", "error": "could not detect caller public IP"}

    already_ok = (
        public_state == "Enabled" and default_action == "Deny" and caller_ip in existing_ips
    )
    if already_ok:
        result = {
            "action": "already_open",
            "ip": caller_ip,
            "public": public_state,
            "default_action": default_action,
            "off_hint": _OFF_HINT,
        }
        with _cache_lock:
            _already_open_cache[cache_key] = (now, result)
        return result

    from azure.mgmt.storage.models import (
        IPRule,
        NetworkRuleSet,
        StorageAccountUpdateParameters,
    )

    new_ip_set = list(dict.fromkeys([*existing_ips, caller_ip]))  # dedupe, preserve order
    vnet_rules = (
        list(getattr(network_rule_set, "virtual_network_rules", None) or [])
        if network_rule_set is not None
        else []
    )
    new_rules = NetworkRuleSet(
        bypass="AzureServices",
        default_action="Deny",
        ip_rules=[IPRule(ip_address_or_range=ip) for ip in new_ip_set],
        virtual_network_rules=vnet_rules,
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

    action = "ip_added" if public_state == "Enabled" else "opened"
    LOGGER.warning(
        "ensure_local_storage_access: %s account=%s ip=%s previous_public=%s "
        "(LOCAL_DEBUG_AUTO_OPEN_STORAGE active — remember to run `%s`)",
        action,
        account_name,
        caller_ip,
        public_state or "Disabled",
        _OFF_HINT,
    )
    result = {
        "action": action,
        "ip": caller_ip,
        "previous_public": public_state or "Disabled",
        "off_hint": _OFF_HINT,
    }
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
