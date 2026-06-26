"""NSG rule helpers for the VNet peering probe flow.

Responsibility: When the post-peering reachability probe fails because an
NSG on the target subnet blocks traffic from the AKS auto-VNet, this
module is the single place that (a) resolves the target subnet's NSG,
(b) checks whether the caller has write permission, and (c) writes an
idempotent inbound-allow security rule. The settings route layer never
imports ``azure.mgmt.*`` directly — every ARM read / mutation flows
through here.

Edit boundaries: ARM reads and writes only. No HTTP shaping, no Celery,
no progress checkpoints. Callable from synchronous FastAPI routes.

Key entry points: ``resolve_nsg_context``, ``has_nsg_write_permission``,
``apply_inbound_allow_rule``.

Risky contracts:
* Source is always derived from the AKS VNet's own ``address_space``
  prefixes. The route layer must never let the caller supply a CIDR —
  that would let an authenticated caller punch a wildcard rule.
* Destination is pinned to ``target_ip/32``. Subnet-wide / wildcard
  destinations are refused before reaching ARM.
* Ports are clamped to the {80, 443} allowlist.
* Rule name is deterministic, so a re-run is an idempotent no-op when
  the rule shape matches. A name-collision whose content differs is
  refused (``conflict_existing`` populated) — operator rules are never
  silently overwritten.
* Permission check uses
  ``AuthorizationManagementClient.permissions.list_for_resource`` on
  the NSG scope and matches the
  ``Microsoft.Network/networkSecurityGroups/securityRules/write``
  action against the returned allow / deny lists (with the standard
  wildcard semantics).

Validation: ``uv run pytest -q api/tests/test_peering_nsg.py``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from api.services.azure_clients import network_client

LOGGER = logging.getLogger(__name__)

ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})
RULE_PRIORITY_MIN = 4000
RULE_PRIORITY_MAX = 4096
RULE_NAME_PREFIX = "elb-dashboard-allow-aks-"
_REQUIRED_ACTION = "Microsoft.Network/networkSecurityGroups/securityRules/write"

# Transient ARM retry knobs. We intentionally do not depend on `tenacity` —
# three attempts with exponential backoff is sufficient for 429 / 5xx
# noise and keeps the dependency surface flat.
_ARM_RETRY_ATTEMPTS = 3
_ARM_RETRY_BASE_SEC = 1.0
_ARM_RETRY_MAX_SEC = 8.0
# Server-supplied `Retry-After` gets its own (higher) cap so we honour
# real throttling guidance instead of hammering ARM every 8s. Anything
# longer than this is treated as a fail-fast signal.
_ARM_RETRY_AFTER_MAX_SEC = 30.0
_ARM_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ServiceRequestError):
        return True
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in _ARM_RETRY_STATUS:
            return True
    return False


def _retry_arm[T](
    fn: Callable[[], T],
    *,
    op_label: str,
    attempts: int = _ARM_RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn()`` with exponential backoff on transient ARM errors.

    Re-raises the original exception unchanged once the budget is
    exhausted so callers can surface real Azure errors verbatim.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not _is_retryable(exc):
                raise
            last = exc
            backoff = min(
                _ARM_RETRY_BASE_SEC * (2 ** (attempt - 1)), _ARM_RETRY_MAX_SEC
            )
            # Pull `Retry-After` when ARM provides it. Honour it with a
            # separate (higher) cap so a real 60s throttle isn't clamped
            # to the 8s backoff ceiling.
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None and retry_after > _ARM_RETRY_AFTER_MAX_SEC:
                # Server is asking for an unreasonably long wait — surface
                # the error so the caller can fail fast instead of
                # silently underwaiting.
                LOGGER.warning(
                    "peering_nsg %s honouring fail-fast on Retry-After=%.1fs (> %.1fs cap)",
                    op_label,
                    retry_after,
                    _ARM_RETRY_AFTER_MAX_SEC,
                )
                raise
            if retry_after is not None:
                delay = retry_after
            else:
                delay = backoff
            # Proportional jitter prevents thundering herd when many
            # callers retry on the same boundary.
            delay += random.uniform(0, max(0.25, delay * 0.1))  # noqa: S311 — jitter, not crypto
            LOGGER.warning(
                "peering_nsg %s transient %s — retrying in %.1fs (attempt %d/%d)",
                op_label,
                type(exc).__name__,
                delay,
                attempt,
                attempts,
            )
            sleep(delay)
    # Defensive: loop above either returns or re-raises; this should be
    # unreachable but keeps mypy happy. Use ``if/raise`` rather than ``assert``
    # so the guard survives Python ``-O``.
    if last is None:  # pragma: no cover
        raise RuntimeError("peering_nsg retry loop exited without recording an exception")
    raise last  # pragma: no cover


def _retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ARM id parsing
# ---------------------------------------------------------------------------


def _parse_vnet_id(vnet_id: str) -> tuple[str, str, str]:
    parts = vnet_id.strip("/").split("/")
    if (
        len(parts) < 8
        or parts[0].lower() != "subscriptions"
        or parts[2].lower() != "resourcegroups"
        or parts[6].lower() != "virtualnetworks"
    ):
        raise ValueError(f"not a VNet ARM id: {vnet_id!r}")
    return parts[1], parts[3], parts[7]


def _parse_nsg_id(nsg_id: str) -> tuple[str, str, str]:
    parts = nsg_id.strip("/").split("/")
    if (
        len(parts) < 8
        or parts[0].lower() != "subscriptions"
        or parts[2].lower() != "resourcegroups"
        or parts[6].lower() != "networksecuritygroups"
    ):
        raise ValueError(f"not an NSG ARM id: {nsg_id!r}")
    return parts[1], parts[3], parts[7]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class NsgContext:
    target_subnet_id: str
    target_subnet_name: str
    target_subnet_prefixes: list[str]
    nsg_id: str | None
    nsg_subscription_id: str | None
    nsg_resource_group: str | None
    nsg_name: str | None
    aks_vnet_address_prefixes: list[str]
    target_ip: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_subnet_id": self.target_subnet_id,
            "target_subnet_name": self.target_subnet_name,
            "target_subnet_prefixes": list(self.target_subnet_prefixes),
            "nsg_id": self.nsg_id,
            "nsg_resource_group": self.nsg_resource_group,
            "nsg_name": self.nsg_name,
            "aks_vnet_address_prefixes": list(self.aks_vnet_address_prefixes),
            "target_ip": self.target_ip,
        }


@dataclass
class ApplyResult:
    applied: bool
    rule_name: str
    nsg_id: str
    priority: int | None = None
    source_prefixes: list[str] = field(default_factory=list)
    destination_ip: str = ""
    ports: list[int] = field(default_factory=list)
    skipped_reason: str | None = None
    conflict_existing: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rule_name": self.rule_name,
            "nsg_id": self.nsg_id,
            "priority": self.priority,
            "source_prefixes": list(self.source_prefixes),
            "destination_ip": self.destination_ip,
            "ports": list(self.ports),
            "skipped_reason": self.skipped_reason,
            "conflict_existing": self.conflict_existing,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _address_prefixes_of_vnet(vnet: Any) -> list[str]:
    """Extract ``address_space.address_prefixes`` from a VNet SDK object.

    SDK models expose ``address_space.address_prefixes``; some serialised
    responses use the dict form ``["addressSpace"]["addressPrefixes"]``.
    Tolerates both because tests stub with bare dicts.
    """
    space = getattr(vnet, "address_space", None)
    if space is not None:
        prefixes = getattr(space, "address_prefixes", None) or []
        return [str(p) for p in prefixes if p]
    if isinstance(vnet, dict):
        space_dict = vnet.get("address_space") or vnet.get("addressSpace") or {}
        prefixes = (
            space_dict.get("address_prefixes")
            or space_dict.get("addressPrefixes")
            or []
        )
        return [str(p) for p in prefixes if p]
    return []


def _subnet_prefixes(subnet: Any) -> list[str]:
    prefixes: list[str] = []
    single = getattr(subnet, "address_prefix", None)
    if single:
        prefixes.append(str(single))
    multi = getattr(subnet, "address_prefixes", None) or []
    prefixes.extend(str(p) for p in multi if p)
    if isinstance(subnet, dict):
        if subnet.get("address_prefix"):
            prefixes.append(str(subnet["address_prefix"]))
        for p in subnet.get("address_prefixes") or []:
            if p:
                prefixes.append(str(p))
    return prefixes


def _subnet_nsg_id(subnet: Any) -> str | None:
    nsg = getattr(subnet, "network_security_group", None)
    if nsg is not None:
        nid = getattr(nsg, "id", None)
        if nid:
            return str(nid)
    if isinstance(subnet, dict):
        nsg_dict = subnet.get("network_security_group") or {}
        if isinstance(nsg_dict, dict) and nsg_dict.get("id"):
            return str(nsg_dict["id"])
    return None


def _ip_in_prefixes(ip: str, prefixes: list[str]) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except (ipaddress.AddressValueError, ValueError):
        return False
    for prefix in prefixes:
        try:
            if addr in ipaddress.IPv4Network(prefix, strict=False):
                return True
        except (ipaddress.AddressValueError, ValueError):
            continue
    return False


def _deterministic_rule_name(aks_vnet_id: str, destination_ip: str) -> str:
    digest = hashlib.sha256(
        f"{aks_vnet_id.lower()}|{destination_ip}".encode()
    ).hexdigest()[:8]
    return f"{RULE_NAME_PREFIX}{digest}"


def _action_matches(pattern: str, action: str) -> bool:
    """Match Azure RBAC action patterns.

    Supported wildcards (per Azure ABAC):
    * ``*`` matches anything,
    * trailing ``/*`` matches the segment and everything below,
    * a single ``*`` mid-pattern is converted to a path-segment regex.
    """
    if pattern == "*":
        return True
    regex = "^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$"
    if re.match(regex, action):
        return True
    # Also accept literal prefix match where pattern ends with ``/*``.
    if pattern.endswith("/*") and action.startswith(pattern[:-1]):
        return True
    return False


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def resolve_vnet_pair_for_cluster(
    cred: TokenCredential,
    *,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    target_subscription_id: str,
    target_resource_group: str,
    target_vnet_name: str,
) -> tuple[str, str]:
    """Return ``(aks_vnet_id, target_vnet_id)`` for a peering pair.

    Raises ``LookupError`` when either side cannot be resolved so the
    route layer can surface a stable 4xx instead of guessing.
    """
    from api.services.azure_clients import aks_client
    from api.tasks.azure.peering import _resolve_aks_vnet_id, _resolve_vnet_id

    aks_cl = aks_client(cred, subscription_id)
    try:
        cluster = aks_cl.managed_clusters.get(cluster_resource_group, cluster_name)
    except Exception as exc:
        raise LookupError(f"aks cluster lookup failed: {type(exc).__name__}") from exc
    node_rg = (getattr(cluster, "node_resource_group", None) or "").strip()
    if not node_rg:
        raise LookupError("aks cluster has no node_resource_group")
    aks_vnet_id = _resolve_aks_vnet_id(
        cred,
        subscription_id=subscription_id,
        node_resource_group=node_rg,
        cluster=cluster,
    )
    if not aks_vnet_id:
        raise LookupError(f"no VNet found in AKS node resource group {node_rg!r}")
    try:
        target_vnet_id = _resolve_vnet_id(
            cred,
            subscription_id=target_subscription_id,
            resource_group=target_resource_group,
            vnet_name=target_vnet_name,
        )
    except Exception as exc:
        raise LookupError(f"target vnet lookup failed: {type(exc).__name__}") from exc
    return aks_vnet_id, target_vnet_id


def resolve_nsg_context(
    cred: TokenCredential,
    *,
    aks_vnet_id: str,
    target_vnet_id: str,
    target_ip: str,
) -> NsgContext | None:
    """Locate the target subnet (and its NSG, if any) for the given IP.

    Returns ``None`` when no subnet of ``target_vnet_id`` contains
    ``target_ip`` — that indicates the operator passed an IP outside the
    VNet, which is a UX bug we want surfaced rather than glossed over.
    """
    # AKS VNet — source CIDRs.
    aks_sub, aks_rg, aks_vnet_name = _parse_vnet_id(aks_vnet_id)
    nc_aks = network_client(cred, aks_sub)
    aks_vnet = nc_aks.virtual_networks.get(aks_rg, aks_vnet_name)
    aks_prefixes = _address_prefixes_of_vnet(aks_vnet)
    if not aks_prefixes:
        LOGGER.warning(
            "peering_nsg: AKS VNet %s has no address_prefixes — refusing",
            aks_vnet_id,
        )
        return None

    # Target VNet — locate subnet containing target_ip.
    tgt_sub, tgt_rg, tgt_vnet_name = _parse_vnet_id(target_vnet_id)
    nc_tgt = network_client(cred, tgt_sub)
    target_vnet = nc_tgt.virtual_networks.get(
        tgt_rg, tgt_vnet_name, expand="subnets"
    )
    subnets = getattr(target_vnet, "subnets", None) or []
    if not subnets and isinstance(target_vnet, dict):
        subnets = target_vnet.get("subnets") or []

    for subnet in subnets:
        prefixes = _subnet_prefixes(subnet)
        if not prefixes:
            continue
        if not _ip_in_prefixes(target_ip, prefixes):
            continue
        subnet_id = str(getattr(subnet, "id", "") or (
            subnet.get("id", "") if isinstance(subnet, dict) else ""
        ))
        subnet_name = str(getattr(subnet, "name", "") or (
            subnet.get("name", "") if isinstance(subnet, dict) else ""
        ))
        nsg_id = _subnet_nsg_id(subnet)
        nsg_sub: str | None = None
        nsg_rg: str | None = None
        nsg_name: str | None = None
        if nsg_id:
            try:
                nsg_sub, nsg_rg, nsg_name = _parse_nsg_id(nsg_id)
            except ValueError as exc:
                LOGGER.warning("peering_nsg: malformed NSG id %r: %s", nsg_id, exc)
                nsg_id = None
        return NsgContext(
            target_subnet_id=subnet_id,
            target_subnet_name=subnet_name,
            target_subnet_prefixes=prefixes,
            nsg_id=nsg_id,
            nsg_subscription_id=nsg_sub,
            nsg_resource_group=nsg_rg,
            nsg_name=nsg_name,
            aks_vnet_address_prefixes=aks_prefixes,
            target_ip=target_ip,
        )
    return None


def has_nsg_write_permission(
    cred: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    nsg_name: str,
    arm_attempts: int | None = None,
) -> bool:
    """Return True iff the caller's effective RBAC allows writing NSG rules.

    Uses ``AuthorizationManagementClient.permissions.list_for_resource``
    which returns the union of role assignments at and above the NSG
    scope. Wildcards (``*``, ``Microsoft.Network/*``, ...) and the
    ``not_actions`` deny list are honoured.
    """
    from api.services.azure_clients import authorization_client

    auth = authorization_client(cred, subscription_id)
    try:
        perms = _retry_arm(
            lambda: list(
                auth.permissions.list_for_resource(
                    resource_group_name=resource_group,
                    resource_provider_namespace="Microsoft.Network",
                    parent_resource_path="",
                    resource_type="networkSecurityGroups",
                    resource_name=nsg_name,
                )
            ),
            op_label="permissions.list_for_resource",
            attempts=arm_attempts if arm_attempts is not None else _ARM_RETRY_ATTEMPTS,
        )
    except Exception as exc:
        LOGGER.warning(
            "peering_nsg: permissions.list_for_resource failed (%s) — assuming no permission",
            exc,
        )
        return False

    for entry in perms:
        actions = list(getattr(entry, "actions", None) or [])
        not_actions = list(getattr(entry, "not_actions", None) or [])
        if isinstance(entry, dict):
            actions = list(entry.get("actions") or [])
            not_actions = list(entry.get("not_actions") or [])
        allowed = any(_action_matches(p, _REQUIRED_ACTION) for p in actions)
        if not allowed:
            continue
        denied = any(_action_matches(p, _REQUIRED_ACTION) for p in not_actions)
        if denied:
            continue
        return True
    return False


def _pick_priority(used: set[int]) -> int | None:
    for prio in range(RULE_PRIORITY_MIN, RULE_PRIORITY_MAX + 1):
        if prio not in used:
            return prio
    return None


def _summarise_rule(existing: Any) -> dict[str, Any]:
    """Return the diagnostic fields needed to render a collision in UI.

    Returned keys mirror the SDK / dict spellings the rest of this
    module reads, so a route can surface them without having to know
    SDK-vs-dict shape.
    """
    def _get(field_name: str) -> Any:
        val = getattr(existing, field_name, None)
        if val is None and isinstance(existing, dict):
            val = existing.get(field_name)
        return val

    src_list = list(_get("source_address_prefixes") or [])
    if _get("source_address_prefix"):
        src_list = [*src_list, str(_get("source_address_prefix"))]
    dst_list = list(_get("destination_address_prefixes") or [])
    if _get("destination_address_prefix"):
        dst_list = [*dst_list, str(_get("destination_address_prefix"))]
    port_list = list(_get("destination_port_ranges") or [])
    if _get("destination_port_range"):
        port_list = [*port_list, str(_get("destination_port_range"))]
    src_port_list = list(_get("source_port_ranges") or [])
    if _get("source_port_range"):
        src_port_list = [*src_port_list, str(_get("source_port_range"))]
    # Singular field is kept for backwards compatibility with the SPA's
    # ConflictExistingPanel rendering; plural is exposed so a rule that
    # uses `destinationAddressPrefixes` (list form) is not silently
    # blanked in the UI.
    singular_dst = _get("destination_address_prefix")
    return {
        "name": _get("name"),
        "priority": _get("priority"),
        "protocol": _get("protocol"),
        "access": _get("access"),
        "direction": _get("direction"),
        "source_address_prefixes": [str(p) for p in src_list],
        "source_port_ranges": [str(p) for p in src_port_list],
        "destination_address_prefix": singular_dst,
        "destination_address_prefixes": [str(p) for p in dst_list],
        "destination_port_ranges": [str(p) for p in port_list],
        "description": _get("description"),
    }


def _existing_matches(
    existing: Any,
    *,
    source_prefixes: list[str],
    destination_ip: str,
    ports: list[int],
) -> bool:
    """Return True when an existing rule already grants what we want.

    Checks protocol (Tcp or *), source-CIDR superset, destination =
    ``target_ip[/32]``, port superset, and access=Allow / direction=Inbound.
    Stricter rules (smaller source set, wider port set) are not considered
    matching — we'd rather no-op the wrong way than silently downgrade.
    """
    existing_src = list(getattr(existing, "source_address_prefixes", None) or [])
    single_src = getattr(existing, "source_address_prefix", None)
    if single_src:
        existing_src.append(str(single_src))
    if isinstance(existing, dict):
        for p in existing.get("source_address_prefixes") or []:
            existing_src.append(str(p))
        if existing.get("source_address_prefix"):
            existing_src.append(str(existing["source_address_prefix"]))
    if {p.strip() for p in source_prefixes} - {p.strip() for p in existing_src}:
        return False

    dest = (
        getattr(existing, "destination_address_prefix", None)
        or (existing.get("destination_address_prefix") if isinstance(existing, dict) else "")
        or ""
    )
    expected_dest = f"{destination_ip}/32"
    if str(dest).strip() not in {destination_ip, expected_dest}:
        return False

    existing_ports = list(getattr(existing, "destination_port_ranges", None) or [])
    single_port = getattr(existing, "destination_port_range", None)
    if single_port:
        existing_ports.append(str(single_port))
    if isinstance(existing, dict):
        for p in existing.get("destination_port_ranges") or []:
            existing_ports.append(str(p))
        if existing.get("destination_port_range"):
            existing_ports.append(str(existing["destination_port_range"]))
    existing_port_set = {str(p).strip() for p in existing_ports}
    required = {str(p) for p in ports}
    if not required.issubset(existing_port_set) and "*" not in existing_port_set:
        return False

    protocol = getattr(existing, "protocol", None) or (
        existing.get("protocol") if isinstance(existing, dict) else ""
    )
    # Azure NSG protocol values: "Tcp" / "Udp" / "Icmp" / "*". "Asterisk" is
    # the API serialisation older SDKs used; treat both as wildcard.
    if str(protocol) not in {"Tcp", "*", "Asterisk"}:
        return False

    access = getattr(existing, "access", None) or (
        existing.get("access") if isinstance(existing, dict) else ""
    )
    direction = getattr(existing, "direction", None) or (
        existing.get("direction") if isinstance(existing, dict) else ""
    )
    if str(access) != "Allow" or str(direction) != "Inbound":
        return False
    return True


def deterministic_rule_name(aks_vnet_id: str, destination_ip: str) -> str:
    """Public alias of the internal deterministic name helper.

    Surfaced so the route layer can render an accurate CLI hint (and
    the dry-run preview) without re-implementing the hashing scheme.
    """
    return _deterministic_rule_name(aks_vnet_id, destination_ip)


def next_free_priority_best_effort(
    cred: TokenCredential,
    *,
    nsg_subscription_id: str,
    nsg_resource_group: str,
    nsg_name: str,
) -> int | None:
    """Return the first free priority in ``[RULE_PRIORITY_MIN, MAX]`` or ``None``.

    Best-effort: callers (e.g. the CLI-hint renderer) want to suggest a
    real next-free priority instead of a hardcoded placeholder, but the
    caller may not have ``securityRules/read``. Any ARM failure is
    swallowed and ``None`` is returned so the caller can fall back to a
    placeholder.
    """
    try:
        nc = network_client(cred, nsg_subscription_id)
        rules = _retry_arm(
            lambda: list(nc.security_rules.list(nsg_resource_group, nsg_name)),
            op_label="security_rules.list (best_effort priority)",
            # Two attempts — one transient 5xx/429 should not collapse us
            # straight to the "could not list" placeholder. The full apply
            # path still gets `_ARM_RETRY_ATTEMPTS=3` budget.
            attempts=2,
        )
    except Exception as exc:
        LOGGER.info(
            "peering_nsg: priority best-effort skipped (%s)", type(exc).__name__
        )
        return None
    used: set[int] = set()
    for rule in rules:
        prio = getattr(rule, "priority", None) or (
            rule.get("priority") if isinstance(rule, dict) else None
        )
        if isinstance(prio, int):
            used.add(prio)
    return _pick_priority(used)


def apply_inbound_allow_rule(
    cred: TokenCredential,
    *,
    nsg_subscription_id: str,
    nsg_resource_group: str,
    nsg_name: str,
    aks_vnet_id: str,
    source_prefixes: list[str],
    destination_ip: str,
    ports: list[int],
    dry_run: bool = False,
    arm_attempts: int | None = None,
) -> ApplyResult:
    """Idempotently write a single inbound-allow rule on the target NSG.

    Refuses to write when the requested rule name is taken by a
    different-content rule. Caller is expected to have already
    validated input (RFC1918 destination, port allowlist, non-empty
    source prefixes); this function adds belt-and-braces checks so a
    direct call from a future caller cannot bypass them.

    When ``dry_run=True`` the function performs every read + collision
    check + priority pick but skips the final ARM ``begin_create_or_update``
    call. The returned ``ApplyResult`` describes what would have been
    written (``applied=False, skipped_reason="dry_run"`` in the success
    branch); idempotent ``already_present`` / ``name_collision`` /
    ``no_free_priority`` branches behave identically with or without
    ``dry_run`` so the preview matches what apply would actually do.
    """
    if not source_prefixes:
        raise ValueError("source_prefixes must not be empty")
    try:
        ipaddress.IPv4Address(destination_ip)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError(f"destination_ip must be IPv4: {destination_ip!r}") from exc
    bad_ports = [p for p in ports if p not in ALLOWED_PORTS]
    if bad_ports:
        raise ValueError(f"ports must be subset of {sorted(ALLOWED_PORTS)}: {bad_ports}")
    if not ports:
        raise ValueError("ports must not be empty")

    attempts = arm_attempts if arm_attempts is not None else _ARM_RETRY_ATTEMPTS

    nsg_id = (
        f"/subscriptions/{nsg_subscription_id}/resourceGroups/{nsg_resource_group}"
        f"/providers/Microsoft.Network/networkSecurityGroups/{nsg_name}"
    )
    rule_name = _deterministic_rule_name(aks_vnet_id, destination_ip)

    nc = network_client(cred, nsg_subscription_id)
    rules = _retry_arm(
        lambda: list(nc.security_rules.list(nsg_resource_group, nsg_name)),
        op_label="security_rules.list",
        attempts=attempts,
    )

    used_priorities: set[int] = set()
    existing_named: Any = None
    for rule in rules:
        prio = getattr(rule, "priority", None) or (
            rule.get("priority") if isinstance(rule, dict) else None
        )
        if isinstance(prio, int):
            used_priorities.add(prio)
        name = getattr(rule, "name", None) or (
            rule.get("name") if isinstance(rule, dict) else ""
        )
        if name == rule_name:
            existing_named = rule

    if existing_named is not None:
        if _existing_matches(
            existing_named,
            source_prefixes=source_prefixes,
            destination_ip=destination_ip,
            ports=ports,
        ):
            prio_val = getattr(existing_named, "priority", None) or (
                existing_named.get("priority") if isinstance(existing_named, dict) else None
            )
            return ApplyResult(
                applied=True,
                rule_name=rule_name,
                nsg_id=nsg_id,
                priority=int(prio_val) if isinstance(prio_val, int) else None,
                source_prefixes=list(source_prefixes),
                destination_ip=destination_ip,
                ports=list(ports),
                skipped_reason="already_present",
            )
        return ApplyResult(
            applied=False,
            rule_name=rule_name,
            nsg_id=nsg_id,
            source_prefixes=list(source_prefixes),
            destination_ip=destination_ip,
            ports=list(ports),
            skipped_reason="name_collision",
            conflict_existing=_summarise_rule(existing_named),
        )

    priority = _pick_priority(used_priorities)
    if priority is None:
        return ApplyResult(
            applied=False,
            rule_name=rule_name,
            nsg_id=nsg_id,
            source_prefixes=list(source_prefixes),
            destination_ip=destination_ip,
            ports=list(ports),
            skipped_reason="no_free_priority",
        )

    body: dict[str, Any] = {
        "protocol": "Tcp",
        "source_port_range": "*",
        "destination_port_ranges": [str(p) for p in ports],
        "source_address_prefixes": list(source_prefixes),
        "destination_address_prefix": f"{destination_ip}/32",
        "access": "Allow",
        "priority": priority,
        "direction": "Inbound",
        "description": (
            "elb-dashboard: probe from AKS auto-VNet to private workload IP"
        ),
    }
    if dry_run:
        LOGGER.info(
            "peering_nsg: dry_run rule %s on %s priority=%s ports=%s",
            rule_name,
            nsg_id,
            priority,
            ports,
        )
        return ApplyResult(
            applied=False,
            rule_name=rule_name,
            nsg_id=nsg_id,
            priority=priority,
            source_prefixes=list(source_prefixes),
            destination_ip=destination_ip,
            ports=list(ports),
            skipped_reason="dry_run",
        )
    poller = _retry_arm(
        lambda: nc.security_rules.begin_create_or_update(
            nsg_resource_group,
            nsg_name,
            rule_name,
            body,
        ),
        op_label="security_rules.begin_create_or_update",
        attempts=attempts,
    )
    # Wrap the LRO wait too. ARM occasionally drops the polling response
    # mid-flight on a 503 / ServiceRequestError; retrying the .result()
    # call (the SDK polls a stable Location URL) is safe because the
    # underlying resource is idempotent on a deterministic name+body.
    _retry_arm(
        poller.result,
        op_label="security_rules.poller.result",
        attempts=attempts,
    )
    LOGGER.info(
        "peering_nsg: applied rule %s on %s priority=%s ports=%s",
        rule_name,
        nsg_id,
        priority,
        ports,
    )
    return ApplyResult(
        applied=True,
        rule_name=rule_name,
        nsg_id=nsg_id,
        priority=priority,
        source_prefixes=list(source_prefixes),
        destination_ip=destination_ip,
        ports=list(ports),
    )
