"""AKS pre-flight availability + quota checks.

Pure-domain helpers that wrap the Microsoft.Compute SDK to answer three
questions the SPA needs *before* it calls `/api/aks/provision`:

1. Which AKS-eligible VM SKUs are actually allowed in this subscription
   in the chosen region? (Without this, the dropdown shows the static
   allow-list and the user only finds out after a ~70 s ARM round-trip
   that the SKU is `NotAvailableForSubscription`.)
2. Does the caller's compute quota have room for the requested core
   count? (Soft warning — Azure may grant a quota request later.)
3. Does the target resource group exist and is the caller allowed to
   read it? (Soft info — the provision task will create it idempotently
   on miss, but we want to tell the user.)

Responsibility: Wrap `azure.mgmt.compute.ComputeManagementClient` +
    `azure.mgmt.resource.ResourceManagementClient` calls in stable
    domain dataclasses suitable for the FE.
Edit boundaries: Pure services. Routes wrap these into HTTP payloads;
    Celery tasks may reuse the same helpers before enqueuing work.
Key entry points: `list_region_sku_availability`, `check_sku_availability`,
    `check_compute_quota`, `check_resource_group_access`,
    `azure_portal_aks_url`.
Risky contracts: Every public function must catch SDK errors and
    return a degraded payload instead of raising — pre-flight failure
    must not 500 the dashboard.
Validation: `uv run pytest -q api/tests/test_aks_availability.py
    api/tests/test_aks_preflight_route.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from api.services.aks_skus import SKU_BY_NAME
from api.services.azure_clients import compute_client, resource_client

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SkuAvailability:
    """Per-SKU availability snapshot for one region."""

    name: str
    available: bool
    reason: str | None = None
    """Machine code from Azure: ``NotAvailableForSubscription``,
    ``QuotaId``, ``NoRegisteredProviderFound``, or our synthetic
    ``UnknownToAzure`` / ``LookupFailed``."""
    location_restricted: bool = False
    """True when the restriction explicitly names the region."""


@dataclass(slots=True)
class QuotaCheck:
    """Result of a quota probe for one (region, SKU-family) pair."""

    family: str
    current: int
    limit: int
    needed: int
    ok: bool
    message: str = ""


@dataclass(slots=True)
class ResourceGroupCheck:
    """Result of probing a target resource group."""

    name: str
    exists: bool
    location: str | None = None
    location_match: bool | None = None
    reason: str | None = None


@dataclass(slots=True)
class PreflightCheck:
    """One row in the modal's pre-flight progress list."""

    name: str
    """Stable id: ``region``, ``skus``, ``quota``, ``resource_group``."""
    status: str
    """``ok`` | ``warn`` | ``fail``."""
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _restriction_reason(restriction: Any) -> tuple[str, bool]:
    """Pull (reason_code, location_specific) out of an Azure restriction."""
    reason_code = getattr(restriction, "reason_code", None) or ""
    rest_type = getattr(restriction, "type", None) or ""
    return str(reason_code or rest_type or "Restricted"), str(rest_type).lower() == "location"


def list_region_sku_availability(
    credential: TokenCredential,
    subscription_id: str,
    region: str,
) -> dict[str, SkuAvailability]:
    """Return availability for every AKS-relevant SKU in the region.

    Azure's `resource_skus.list` enumerates *every* compute SKU offered in
    the region with `restrictions` populated for any that are blocked.
    We intersect with `SKU_BY_NAME` so the FE only sees rows for SKUs we
    actually allow in `provision_aks`. SKUs missing from Azure's listing
    are reported as `UnknownToAzure` — usually means the SKU is a brand
    new family that has not rolled out to the region yet.
    """
    result: dict[str, SkuAvailability] = {}
    region_lower = (region or "").lower()
    try:
        client = compute_client(credential, subscription_id)
        # `filter` is documented but the SDK silently ignores unknown
        # filter keys, so we still loop and check `locations` manually.
        seen: set[str] = set()
        for sku in client.resource_skus.list(filter=f"location eq '{region_lower}'"):
            if (getattr(sku, "resource_type", None) or "") != "virtualMachines":
                continue
            name = getattr(sku, "name", None) or ""
            if name not in SKU_BY_NAME:
                continue
            locations = [str(loc).lower() for loc in (getattr(sku, "locations", None) or [])]
            if region_lower and region_lower not in locations:
                continue
            restrictions = getattr(sku, "restrictions", None) or []
            blocking = [r for r in restrictions if _restriction_reason(r)]
            if blocking:
                reason, loc_specific = _restriction_reason(blocking[0])
                result[name] = SkuAvailability(
                    name=name,
                    available=False,
                    reason=reason,
                    location_restricted=loc_specific,
                )
            else:
                result[name] = SkuAvailability(name=name, available=True)
            seen.add(name)
        # Any allow-listed SKU that did not appear at all is `UnknownToAzure`.
        for name in SKU_BY_NAME:
            if name not in seen:
                result[name] = SkuAvailability(
                    name=name,
                    available=False,
                    reason="UnknownToAzure",
                )
    except HttpResponseError as exc:
        LOGGER.warning("resource_skus.list failed for %s: %s", region, exc.message)
    except Exception as exc:
        LOGGER.warning("resource_skus.list crashed for %s: %s", region, type(exc).__name__)
    return result


def check_sku_availability(
    credential: TokenCredential,
    subscription_id: str,
    region: str,
    sku_names: list[str],
) -> dict[str, SkuAvailability]:
    """Return availability only for the requested SKUs.

    Reuses `list_region_sku_availability` for one region, so callers can
    batch many SKU checks (e.g. system + workload pool) without paying for
    two `resource_skus.list` round-trips.
    """
    full = list_region_sku_availability(credential, subscription_id, region)
    out: dict[str, SkuAvailability] = {}
    for name in sku_names:
        if not name:
            continue
        out[name] = full.get(
            name,
            SkuAvailability(
                name=name,
                available=False,
                reason="LookupFailed",
            ),
        )
    return out


# Map SKU name -> ARM usage `name.value` for family-level vCPU quota.
# The usage name is the **SKU family** string Azure returns from
# `compute.usage.list(region)`. We only need to handle the families
# referenced by our allow-list; anything else falls back to the
# `cores` (Total Regional vCPUs) bucket.
_SKU_FAMILY_USAGE: dict[str, str] = {
    "Standard_D2s_v3": "standardDSv3Family",
    "Standard_D4s_v3": "standardDSv3Family",
    "Standard_D8s_v3": "standardDSv3Family",
    "Standard_D16s_v3": "standardDSv3Family",
    "Standard_E8s_v3": "standardESv3Family",
    "Standard_E16s_v3": "standardESv3Family",
    "Standard_E32s_v3": "standardESv3Family",
    "Standard_E8s_v5": "standardESv5Family",
    "Standard_E16s_v5": "standardESv5Family",
    "Standard_E32s_v5": "standardESv5Family",
    "Standard_E48s_v5": "standardESv5Family",
    "Standard_E64s_v5": "standardESv5Family",
    "Standard_E8bs_v5": "standardEBSv5Family",
    "Standard_E16bs_v5": "standardEBSv5Family",
    "Standard_E32bs_v5": "standardEBSv5Family",
    "Standard_E64bs_v5": "standardEBSv5Family",
    "Standard_HB120rs_v3": "standardHBSv3Family",
    "Standard_L8s_v3": "standardLSv3Family",
    "Standard_L16s_v3": "standardLSv3Family",
    "Standard_L32s_v3": "standardLSv3Family",
    "Standard_L64s_v3": "standardLSv3Family",
}

_TOTAL_REGIONAL_VCPU = "cores"


def check_compute_quota(
    credential: TokenCredential,
    subscription_id: str,
    region: str,
    sku_to_count: dict[str, int],
) -> list[QuotaCheck]:
    """Return per-family quota deltas for the requested SKUs.

    `sku_to_count` is e.g. ``{"Standard_E16s_v5": 10, "Standard_D2s_v3": 1}``.
    We aggregate needed cores per family then look up the current quota
    via `compute.usage.list(region)`. The total-regional-vCPU bucket is
    always checked too — even if every family bucket has room, the
    region-wide cap can still block the create.
    """
    if not sku_to_count:
        return []
    # Aggregate needed cores per usage family.
    needed_by_family: dict[str, int] = {}
    total_needed = 0
    for sku_name, count in sku_to_count.items():
        spec = SKU_BY_NAME.get(sku_name)
        if not spec or count <= 0:
            continue
        cores = int(spec.vcpus) * int(count)
        total_needed += cores
        family = _SKU_FAMILY_USAGE.get(sku_name)
        if family:
            needed_by_family[family] = needed_by_family.get(family, 0) + cores
    if not needed_by_family and total_needed == 0:
        return []
    # Look up usage. Keyed by `name.value`.
    usages: dict[str, tuple[int, int]] = {}
    try:
        client = compute_client(credential, subscription_id)
        for u in client.usage.list(region):
            key = getattr(getattr(u, "name", None), "value", None) or ""
            if not key:
                continue
            current = int(getattr(u, "current_value", 0) or 0)
            limit = int(getattr(u, "limit", 0) or 0)
            usages[key] = (current, limit)
    except HttpResponseError as exc:
        LOGGER.warning("compute.usage.list failed for %s: %s", region, exc.message)
        return []
    except Exception as exc:
        LOGGER.warning("compute.usage.list crashed for %s: %s", region, type(exc).__name__)
        return []
    checks: list[QuotaCheck] = []
    for family, needed in needed_by_family.items():
        current, limit = usages.get(family, (0, 0))
        ok = (current + needed) <= limit if limit else False
        checks.append(
            QuotaCheck(
                family=family,
                current=current,
                limit=limit,
                needed=needed,
                ok=ok,
                message=(
                    f"{family}: needs {needed} cores; have {limit - current} of {limit} free"
                    if limit
                    else f"{family}: quota not reported by Azure"
                ),
            )
        )
    if total_needed > 0 and _TOTAL_REGIONAL_VCPU in usages:
        current, limit = usages[_TOTAL_REGIONAL_VCPU]
        ok = (current + total_needed) <= limit if limit else False
        checks.append(
            QuotaCheck(
                family="Total Regional vCPUs",
                current=current,
                limit=limit,
                needed=total_needed,
                ok=ok,
                message=(
                    f"Total regional vCPUs: needs {total_needed}; "
                    f"have {limit - current} of {limit} free"
                ),
            )
        )
    return checks


def check_resource_group_access(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
) -> ResourceGroupCheck:
    """Probe the target RG and report exists / location-match."""
    if not resource_group:
        return ResourceGroupCheck(
            name="",
            exists=False,
            reason="empty",
        )
    try:
        rc = resource_client(credential, subscription_id)
        rg = rc.resource_groups.get(resource_group)
    except ResourceNotFoundError:
        return ResourceGroupCheck(name=resource_group, exists=False)
    except HttpResponseError as exc:
        return ResourceGroupCheck(
            name=resource_group,
            exists=False,
            reason=f"{exc.status_code or ''} {exc.reason or ''}".strip() or "AccessDenied",
        )
    except Exception as exc:
        return ResourceGroupCheck(
            name=resource_group,
            exists=False,
            reason=type(exc).__name__,
        )
    location = (getattr(rg, "location", None) or "").lower()
    match = (location == (region or "").lower()) if location and region else None
    return ResourceGroupCheck(
        name=resource_group,
        exists=True,
        location=location,
        location_match=match,
    )


def azure_portal_aks_url(
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> str:
    """Deep-link to the AKS overview blade in the Azure portal.

    The user-friendly portal URL the blade itself uses. Same shape that
    `az aks show --query id` would produce, wrapped in the portal's
    `#@/resource/...` deep-link shell.
    """
    arm_id = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
    )
    return f"https://portal.azure.com/#@/resource{arm_id}/overview"


def run_provision_preflight(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    region: str,
    node_sku: str,
    node_count: int,
    system_vm_size: str,
    system_node_count: int,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
) -> tuple[bool, list[PreflightCheck]]:
    """Compose SKU + quota + RG checks into a single ordered list.

    Returns `(ok, checks)`. `ok=False` means at least one `fail`-status
    row is present — the route layer maps this to a 400 with the same
    payload so the FE can render it inline.
    """
    checks: list[PreflightCheck] = []
    overall_ok = True

    # ---- SKUs ----
    sku_names = [s for s in (system_vm_size, node_sku) if s]
    sku_results = check_sku_availability(credential, subscription_id, region, sku_names)
    unavailable = [s for s in sku_results.values() if not s.available]
    if not sku_results:
        checks.append(
            PreflightCheck(
                name="skus",
                status="warn",
                message=(
                    f"Could not verify SKU availability in {region} "
                    f"(Azure listing failed). Proceeding may still hit a BadRequest."
                ),
                details={"requested": sku_names},
            )
        )
    elif unavailable:
        overall_ok = False
        names = ", ".join(u.name for u in unavailable)
        reasons = sorted({u.reason or "Restricted" for u in unavailable})
        checks.append(
            PreflightCheck(
                name="skus",
                status="fail",
                message=(
                    f"{names} not available in {region}: {', '.join(reasons)}. "
                    f"Pick a different SKU or region."
                ),
                details={
                    "unavailable": [
                        {
                            "name": s.name,
                            "reason": s.reason,
                            "location_restricted": s.location_restricted,
                        }
                        for s in unavailable
                    ],
                },
            )
        )
    else:
        checks.append(
            PreflightCheck(
                name="skus",
                status="ok",
                message=f"All requested VM SKUs are available in {region}.",
                details={"verified": [s.name for s in sku_results.values()]},
            )
        )

    # ---- Quota ----
    # The blast pool's per-node vCPU count anchors the FE's "max nodes
    # that fit" suggestion (P1-3), so we surface it in the quota row's
    # `details` whenever we have a shortfall.
    blast_spec = SKU_BY_NAME.get(node_sku)
    sys_spec = SKU_BY_NAME.get(system_vm_size)
    blast_cores_per_node = int(blast_spec.vcpus) if blast_spec else 0
    sys_cores_per_node = int(sys_spec.vcpus) if sys_spec else 0
    sys_cores_total = sys_cores_per_node * max(1, int(system_node_count or 1))

    sku_to_count: dict[str, int] = {}
    if system_vm_size:
        sku_to_count[system_vm_size] = sku_to_count.get(system_vm_size, 0) + max(
            1, int(system_node_count or 1)
        )
    if node_sku:
        sku_to_count[node_sku] = sku_to_count.get(node_sku, 0) + max(1, int(node_count or 1))
    quota = check_compute_quota(credential, subscription_id, region, sku_to_count)
    quota_failures = [q for q in quota if not q.ok and q.limit > 0]
    if not quota:
        # Azure didn't return quota numbers at all (transient outage,
        # missing permissions, or a region we cannot read usage for).
        # We keep this as a soft warning — the canonical ARM error is
        # still the ground truth.
        checks.append(
            PreflightCheck(
                name="quota",
                status="warn",
                message=(
                    "Could not read compute quota for "
                    f"{region}. Submitting anyway; Azure will validate at provision time."
                ),
            )
        )
    elif quota_failures:
        # Quota shortfall is a hard ARM-enforced limit (regional and
        # per-family vCPU caps). Re-running with the same payload will
        # always be rejected at the ~70 s mark with
        # `ErrCode_InsufficientVCPUQuota`, so we block submit and ask
        # the user to either request a quota increase or shrink the
        # cluster. We compute the largest blast `node_count` that fits
        # so the FE can offer a one-click "Apply" fix without the user
        # having to do the math.
        names = "; ".join(q.message for q in quota_failures)
        max_blast_nodes: int | None = None
        binding_family: str | None = None
        if blast_cores_per_node > 0:
            # Find the tightest constraint across every shortfall row.
            for q in quota_failures:
                free = max(0, q.limit - q.current)
                # The system pool's cores need to fit too when the same
                # family hosts both pools (e.g. system & blast both on
                # Standard DSv3 family). Subtract that overhead.
                family_takes_system = (
                    q.family != "Total Regional vCPUs"
                    and _SKU_FAMILY_USAGE.get(system_vm_size, "") == q.family
                )
                if q.family == "Total Regional vCPUs":
                    blast_budget = free - sys_cores_total
                elif family_takes_system:
                    blast_budget = free - sys_cores_total
                else:
                    # Family-specific row that holds only the blast pool.
                    blast_budget = free
                fit = blast_budget // blast_cores_per_node
                if fit < 0:
                    fit = 0
                if max_blast_nodes is None or fit < max_blast_nodes:
                    max_blast_nodes = fit
                    binding_family = q.family
        checks.append(
            PreflightCheck(
                name="quota",
                status="fail",
                message=(
                    "Quota too small for the requested cluster in "
                    f"{region}. {names}."
                ),
                details={
                    "shortfall": [
                        {
                            "family": q.family,
                            "current": q.current,
                            "limit": q.limit,
                            "needed": q.needed,
                            "free": max(0, q.limit - q.current),
                        }
                        for q in quota_failures
                    ],
                    "blast_cores_per_node": blast_cores_per_node,
                    "system_cores_total": sys_cores_total,
                    "max_blast_nodes_fit": max_blast_nodes,
                    "binding_family": binding_family,
                },
            )
        )
    else:
        checks.append(
            PreflightCheck(
                name="quota",
                status="ok",
                message="Compute quota is sufficient.",
            )
        )

    # ---- Resource group ----
    rg = check_resource_group_access(credential, subscription_id, resource_group, region)
    if rg.exists and rg.location_match is False:
        checks.append(
            PreflightCheck(
                name="resource_group",
                status="warn",
                message=(
                    f"Resource group '{rg.name}' is in {rg.location}, but the cluster "
                    f"will be created in {region}. This is allowed but unusual."
                ),
                details={"resource_group_location": rg.location, "cluster_region": region},
            )
        )
    elif rg.exists:
        checks.append(
            PreflightCheck(
                name="resource_group",
                status="ok",
                message=f"Resource group '{rg.name}' exists and will be reused.",
                details={"resource_group_location": rg.location},
            )
        )
    elif rg.reason:
        # Reachable but errored — likely missing read RBAC. Not a hard
        # fail because the provision task will retry creation under MI.
        checks.append(
            PreflightCheck(
                name="resource_group",
                status="warn",
                message=(
                    f"Could not read resource group '{rg.name}' ({rg.reason}); "
                    f"will attempt to create it on provision."
                ),
            )
        )
    else:
        checks.append(
            PreflightCheck(
                name="resource_group",
                status="ok",
                message=f"Resource group '{rg.name}' will be created.",
            )
        )

    # ---- RBAC ----
    # Verify the dashboard MI has the role assignments AKS create needs
    # (Contributor on the cluster RG + sub-scope RG-write). The check
    # never raises — a missing read-permission or absent principal id
    # degrades to a warn row so preflight can still render the SKU /
    # quota / RG outcomes. Import inside the function so the module's
    # top-level import graph stays cycle-free.
    from api.services.rbac_preflight import (
        aks_create_rbac_check,
        aks_runtime_rbac_check,
    )

    checks.append(
        aks_create_rbac_check(
            credential,
            subscription_id=subscription_id,
            resource_group=resource_group,
        )
    )

    # Runtime RBAC advisory — UAA on ACR + Storage scopes. Always emitted
    # so the operator sees explicitly whether the post-create role
    # assignment step will succeed. `warn` (not `fail`) because Azure
    # RBAC is still the ground truth and the provision task fail-fasts
    # on the actual failure (see ensuring_rbac in provision_aks).
    checks.append(
        aks_runtime_rbac_check(
            credential,
            subscription_id=subscription_id,
            resource_group=resource_group,
            acr_resource_group=acr_resource_group,
            acr_name=acr_name,
            storage_resource_group=storage_resource_group,
            storage_account=storage_account,
        )
    )

    # Derive overall_ok from the rendered rows so we don't have to keep
    # the manual flag in sync with every new `fail` site below. Any
    # `fail` row anywhere blocks submit; `warn` is informational.
    if any(c.status == "fail" for c in checks):
        overall_ok = False
    return overall_ok, checks
