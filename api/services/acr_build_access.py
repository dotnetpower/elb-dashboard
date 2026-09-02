"""Lease temporary public ACR access for Microsoft-managed build agents.

Responsibility: Open one registry for an ACR Task, verify propagation, then
restore private steady state without interrupting another active build.
Edit boundaries: Azure SDK network-policy operations only; tasks own build
scheduling, polling, progress, and deploy decisions.
Key entry points: `open_build_access`, `restore_build_access`.
Risky contracts: Every wait is bounded; unknown/active build state fails safe by
leaving access open; a restore failure is returned to the caller and must block
downstream deployment; Storage is never touched.
Validation: `uv run pytest -q api/tests/test_acr_build_access_service.py`.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential
from azure.mgmt.containerregistry import ContainerRegistryManagementClient
from azure.mgmt.containerregistry.models import IPRule, NetworkRuleSet, RegistryUpdateParameters

from api.services.azure_clients import acr_client

LOGGER = logging.getLogger(__name__)
_ACTIVE_BUILD_STATES = frozenset({"Queued", "Started", "Running"})
_BUILD_API_VERSION = "2019-06-01-preview"


def _enum_value(value: object, default: str) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text or default


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class BuildAccessLease:
    subscription_id: str
    resource_group: str
    registry_name: str
    original_public: str
    original_default_action: str
    original_bypass: str
    original_ip_rules: tuple[str, ...]
    restore_required: bool
    restore_to_private: bool


def _network_state(registry: Any) -> tuple[str, str, str]:
    rules = getattr(registry, "network_rule_set", None)
    return (
        _enum_value(getattr(registry, "public_network_access", None), "Disabled"),
        _enum_value(getattr(rules, "default_action", None), "Deny"),
        _enum_value(getattr(registry, "network_rule_bypass_options", None), "AzureServices"),
    )


def _ip_rules(registry: Any) -> tuple[str, ...]:
    rules = getattr(getattr(registry, "network_rule_set", None), "ip_rules", None) or ()
    return tuple(
        sorted(
            str(getattr(rule, "ip_address_or_range", "") or "").strip()
            for rule in rules
            if str(getattr(rule, "ip_address_or_range", "") or "").strip()
        )
    )


def _has_approved_private_endpoint(registry: Any) -> bool:
    connections = getattr(registry, "private_endpoint_connections", None) or ()
    for connection in connections:
        state = getattr(connection, "private_link_service_connection_state", None)
        if _enum_value(getattr(state, "status", None), "") == "Approved":
            return True
    return False


def _update_network(
    client: Any,
    resource_group: str,
    registry_name: str,
    *,
    public: str,
    default_action: str,
    bypass: str,
    ip_rules: tuple[str, ...] = (),
) -> None:
    poller = client.registries.begin_update(
        resource_group,
        registry_name,
        RegistryUpdateParameters(
            public_network_access=public,
            network_rule_set=NetworkRuleSet(
                default_action=default_action,
                ip_rules=[IPRule(ip_address_or_range=value) for value in ip_rules],
            ),
            network_rule_bypass_options=bypass,
        ),
    )
    poller.result(timeout=120)


def open_build_access(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
    max_attempts: int = 18,
    interval_seconds: float = 5.0,
    settle_seconds: float = 75.0,
) -> BuildAccessLease:
    """Open build access and return the restoration lease."""
    client = acr_client(credential, subscription_id)
    registry = client.registries.get(resource_group, registry_name)
    public, default_action, bypass = _network_state(registry)
    original_ip_rules = _ip_rules(registry)
    already_open = public == "Enabled" and default_action == "Allow" and bypass == "AzureServices"
    preserve_open = _truthy_env("ACR_BUILD_ACCESS_PRESERVE_OPEN")
    private_endpoint_ready = _has_approved_private_endpoint(registry)
    if already_open and not preserve_open and not private_endpoint_ready:
        raise RuntimeError(
            "ACR build access is already public and no approved private endpoint "
            "is available for safe restoration"
        )
    heal_stale_open = already_open and not preserve_open
    lease = BuildAccessLease(
        subscription_id=subscription_id,
        resource_group=resource_group,
        registry_name=registry_name,
        original_public=public,
        original_default_action=default_action,
        original_bypass=bypass,
        original_ip_rules=original_ip_rules,
        restore_required=not already_open or heal_stale_open,
        restore_to_private=heal_stale_open,
    )
    if already_open:
        LOGGER.warning(
            "ACR build access already open registry=%s preserve=%s private_endpoint_ready=%s",
            registry_name,
            preserve_open,
            private_endpoint_ready,
        )
        return lease

    _update_network(
        client,
        resource_group,
        registry_name,
        public="Enabled",
        default_action="Allow",
        bypass="AzureServices",
        ip_rules=original_ip_rules,
    )
    try:
        for _attempt in range(1, max(1, max_attempts) + 1):
            current = client.registries.get(resource_group, registry_name)
            if _network_state(current) == ("Enabled", "Allow", "AzureServices"):
                if settle_seconds > 0:
                    time.sleep(settle_seconds)
                return lease
            if interval_seconds > 0:
                time.sleep(interval_seconds)
        raise RuntimeError("ACR build access policy did not become effective")
    except Exception:
        try:
            _update_network(
                client,
                resource_group,
                registry_name,
                public=public,
                default_action=default_action,
                bypass=bypass,
                ip_rules=original_ip_rules,
            )
        except Exception as restore_exc:
            LOGGER.critical(
                "ACR build access open rollback failed registry=%s error=%s",
                registry_name,
                type(restore_exc).__name__,
            )
        raise


def _active_build_count(
    credential: TokenCredential,
    lease: BuildAccessLease,
) -> int:
    client = ContainerRegistryManagementClient(
        credential,
        lease.subscription_id,
        api_version=_BUILD_API_VERSION,
    )
    count = 0
    try:
        for run in client.runs.list(
            lease.resource_group,
            lease.registry_name,
            top=100,
            connection_timeout=10,
            read_timeout=20,
        ):
            status = _enum_value(getattr(run, "status", None), "")
            if status in _ACTIVE_BUILD_STATES:
                count += 1
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return count


def restore_build_access(
    credential: TokenCredential,
    lease: BuildAccessLease,
) -> bool:
    """Restore the lease; return False when safety requires leaving it open."""
    if not lease.restore_required:
        return True
    try:
        active = _active_build_count(
            credential,
            lease,
        )
    except Exception as exc:
        LOGGER.error(
            "ACR active build query failed registry=%s error=%s",
            lease.registry_name,
            type(exc).__name__,
        )
        return False
    if active:
        LOGGER.warning(
            "ACR build access restore deferred registry=%s active_builds=%s",
            lease.registry_name,
            active,
        )
        return False

    public = "Disabled" if lease.restore_to_private else lease.original_public
    default_action = "Deny" if lease.restore_to_private else lease.original_default_action
    bypass = "AzureServices" if lease.restore_to_private else lease.original_bypass
    ip_rules = () if lease.restore_to_private else lease.original_ip_rules
    try:
        client = acr_client(credential, lease.subscription_id)
        _update_network(
            client,
            lease.resource_group,
            lease.registry_name,
            public=public,
            default_action=default_action,
            bypass=bypass,
            ip_rules=ip_rules,
        )
        current = client.registries.get(lease.resource_group, lease.registry_name)
        restored = (
            _network_state(current) == (public, default_action, bypass)
            and _ip_rules(current) == ip_rules
        )
    except Exception as exc:
        LOGGER.error(
            "ACR build access restore failed registry=%s error=%s",
            lease.registry_name,
            type(exc).__name__,
        )
        return False
    if not restored:
        LOGGER.error(
            "ACR build access restore verification failed registry=%s", lease.registry_name
        )
    return restored
