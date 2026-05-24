# AKS Provisioning — Pre-flight, Region-filtered SKU Picker, Portal Link

## Motivation

Symptom users hit today:

> Click `Create Cluster` with `Standard_E16s_v5` in `koreacentral` →
> banner shows `Provisioning…` for ~1 minute 10 seconds → task fails
> with `BadRequest: The VM size of Standard_E16s_v5, Standard_D2s_v3
> is not allowed in your subscription in location 'koreacentral'`.

Three independent problems combined to produce that UX:

1. The SKU dropdown showed the **static allow-list** (`api/services/aks_skus.py`),
   not the SKUs actually deployable in the user's subscription × region.
   Azure restricts SKUs per-subscription-per-region (new / unverified
   subscriptions especially); the allow-list contains SKUs Azure had
   never enabled for the caller in `koreacentral`.
2. There was **no pre-flight**: the FE went straight from "Create
   Cluster" click to enqueuing the Celery `provision_aks` task, which
   only learned about the SKU block after the ~70 s ARM PUT round trip.
3. The banner had **no path to the Azure portal**, so even when the
   task did run cleanly the user could not click through to watch the
   ARM resource in the portal.

## User-facing change

### 1. Region-filtered SKU picker
Opening the Create modal now silently fetches `/api/aks/available-skus?
subscription_id=…&region=…`. SKUs the caller cannot deploy in that
region are still listed (so the user understands what is missing) but
appear as `Standard_E16s_v5 — not available in koreacentral`, are
`disabled` in the `<select>`, and carry a `title=` tooltip with the
reason code (`NotAvailableForSubscription` / `QuotaId` /
`NoRegisteredProviderFound` / `UnknownToAzure`). Changing region
refetches and re-filters live.

### 2. Pre-flight gate before provision
Clicking `Create Cluster` no longer enqueues the task immediately.
Instead the modal calls `POST /api/aks/preflight` and renders the
result inline:

- `VM SKU availability — All requested VM SKUs are available in koreacentral.` ✓
- `Compute quota — Quota may be insufficient. standardESv5Family: needs 160 cores; have 0 of 100 free` ⚠
- `Resource group — Resource group 'rg-elb-cluster' exists and will be reused.` ✓

`fail` rows (SKU block, etc.) block the submit button until inputs
change. `warn` rows (quota shortfall, RG region mismatch) inform but
pass through — the canonical ARM error is still the ground truth.
Editing any input (region, RG, SKU, count) invalidates the cached
pre-flight, forcing a fresh check.

The 70-second `BadRequest` round trip is gone — the user sees the
"not available" message within ~1 s of clicking, with actionable
copy.

### 3. Azure Portal deep link
Once `provision_aks` sees the cluster appear in ARM (first successful
`aks.managed_clusters.get`), the Celery progress payload now carries
a `portal_url` (e.g. `https://portal.azure.com/#@/resource/subscriptions/…/managedClusters/elb-cluster-01/overview`).
The banner renders an `Open in Azure portal ↗` chip that opens in a
new tab with `rel="noopener noreferrer"`. The link is also rendered
on the `completed` tick so the user can click through after the cluster
is ready.

## API / IaC diff

### New backend modules
- `api/services/aks_availability.py` — pure-domain helpers:
  - `list_region_sku_availability(credential, subscription_id, region) → dict[str, SkuAvailability]`
    via `ComputeManagementClient.resource_skus.list`. Intersects with
    `SKU_BY_NAME` so the FE only sees allow-listed rows.
  - `check_sku_availability(...)` — single-region batch lookup.
  - `check_compute_quota(...)` via `ComputeManagementClient.usage.list(region)`,
    aggregates needed cores per SKU family (`standardESv5Family`,
    `standardDSv3Family`, …) and the total-regional-vCPU bucket.
  - `check_resource_group_access(...)` via `ResourceManagementClient`.
  - `run_provision_preflight(...)` composes the three into an ordered
    `list[PreflightCheck]` for the FE.
  - `azure_portal_aks_url(subscription_id, resource_group, cluster_name)`
    builds the canonical portal deep link.
- `api/routes/aks/preflight.py` — two thin HTTP wrappers:
  - `GET  /api/aks/available-skus?subscription_id=…&region=…`
  - `POST /api/aks/preflight`
- Both registered in `api/routes/aks/__init__.py` (above the existing
  `_provision_routes` include — order matches the rest of the package).
- `api/tests/test_aks_availability.py` — 7 unit + route tests covering
  blocked SKU, family quota shortfall, RG existing, run_preflight fail
  path, portal URL shape, and TestClient round trips for both routes.

### Backend task changes
- `api/tasks/azure/provision.py::_poll_arm_create`: now accepts
  `subscription_id`, and on the first successful `managed_clusters.get`
  computes and pins a `portal_url` that is included in every subsequent
  `arm_create_or_update` progress publish.
- `provision_aks`: also publishes `portal_url` on the initial
  `arm_create_or_update` "Submitting…" tick and on the final `completed`
  tick.

### Frontend changes
- `web/src/api/aks.ts`: added `AksAvailableSkusResponse`,
  `AksPreflightRequest`, `AksPreflightCheck`, `AksPreflightResponse`
  types; added `aksApi.availableSkus(…)` and `aksApi.preflight(…)`.
- `web/src/hooks/useAksSkus.ts`: added `useAksAvailableSkus({
  subscriptionId, region, allSkus })` returning `availableSet`,
  `unavailableMap`, `degraded`, `isLoading`. Permissive on `degraded`
  (better to let the canonical ARM error through than to hide every
  SKU).
- `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`:
  added `preflightStatus` / `preflightResult` state; `handleProvision`
  is now a two-stage flow (preflight → on success, enqueue provision).
  Any input change that affects pre-flight clears the cached result.
- `web/src/components/cards/ClusterCard/ProvisionModal.tsx`:
  - New props: `availableSkusSet`, `unavailableSkusMap`,
    `availabilityLoading`, `availabilityDegraded`, `preflightStatus`,
    `preflightResult`.
  - Both `<select>`s render `disabled` + tooltip + `— not available in
    <region>` suffix for SKUs not in `availableSkusSet`.
  - New pre-flight progress panel above the sticky footer with per-row
    status icons (CheckCircle2 / AlertTriangle / AlertCircle).
  - `Create Cluster` button switches to `Validating…` while preflight is
    in flight and to `Fix errors above` when `preflightResult.ok ===
    false`. Submit guard mirrors the disabled conditions.
- `web/src/components/cards/ClusterCard/ProvisioningBanner.tsx`:
  - `ProvisionProgress` interface gained `portal_url?: string | null`.
  - New "Open in Azure portal" chip rendered when `portal_url` is set.
- `web/src/components/cards/ClusterCard/ClusterCard.tsx`: wires the
  new hook + new props into the modal.

No infra changes. No new dependencies.

## Validation

- `uv run pytest -q api/tests/test_aks_availability.py
  api/tests/test_azure_provision_aks.py api/tests/test_azure_tasks.py` —
  18 passed.
- `uv run ruff check api/services/aks_availability.py
  api/routes/aks/preflight.py api/routes/aks/__init__.py
  api/tasks/azure/provision.py api/tests/test_aks_availability.py` —
  clean (after autofix of two unused `noqa` markers).
- `cd web && npm run build` — built in 6.13 s, no TypeScript errors.
- Manual scenario (the reported case):
  - Open Create modal → pick `koreacentral` → the dropdown now
    immediately greys out `Standard_E16s_v5` with
    `— not available in koreacentral`.
  - Pick an available SKU instead → click `Create Cluster` → the
    pre-flight panel renders the three rows in ~1 s; SKU = ✓, quota
    = ⚠ (if applicable), RG = ✓ → provision proceeds.
  - On the running banner the `Open in Azure portal` chip appears as
    soon as the ARM resource is visible (~20–40 s after the PUT).
