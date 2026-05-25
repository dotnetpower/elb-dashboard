# AKS Cluster Card: subscription-wide list + `elb-tier` classification

## Motivation

ClusterCard previously listed only AKS clusters in the card's *workload
resource group* (the same RG used as the provisioning anchor), and grafted
a fragile `recentProvisionAttempt` localStorage union on top so a
just-provisioned cluster in a *different* RG would still appear for the
~5 min that ARM tracking lasted. That union had three problems:

1. **Single-browser scope.** Another operator on the same Azure tenant
   never saw the in-flight provision.
2. **Stale after TTL.** A cluster that succeeded in a non-anchor RG fell
   out of the list once the localStorage entry expired, even though
   it was still there in ARM.
3. **No multi-cluster operating model.** Real BLAST fleets run
   *multiple* AKS clusters at once — typically a `heavy` cluster for
   GB-scale queries on big databases and a `light` cluster for short
   nucleotide runs. The RG-scoped list could only show one of them.

This change moves the dashboard to a **subscription-wide AKS list,
filtered to ElasticBLAST-managed clusters** by ARM tags written at
provision time, and adds an optional `elb-tier` tag so the operator
can label each cluster as `heavy` / `light` / `gpu` / `general`.

## User-facing change

* The ClusterCard now lists **every ELB-managed cluster in the selected
  subscription**, regardless of which RG it lives in. Each row shows
  the cluster name, an optional tier badge, and the cluster's own
  resource group next to the name. Start/stop/delete/autoWarmup
  actions automatically target the cluster's own RG.
* The card subtitle becomes `Subscription-wide · anchor: <rg>` (the
  anchor RG is still used as the default *provision* target and as
  the scope for storage/ACR/terminal context).
* The provision modal has a new **Tier** dropdown
  (`(none)` / `heavy` / `light` / `gpu` / `general`). Selecting a tier
  writes the value into the `elb-tier` ARM tag on the new cluster;
  leaving it `(none)` skips the tag entirely. The tag is just metadata
  — it does not change any sizing or scheduling logic on its own.

## API / IaC diff summary

### Backend

* New service `api.services.monitoring.list_aks_clusters_in_subscription(credential, sub, *, include_unmanaged=False)`:
  * Calls `ManagedClusters.list()` (sub-wide), parses the ARM id for
    per-row `resource_group`, and runs every cluster through
    `_is_elb_managed_cluster()`.
  * Filter contract (load-bearing security): returns True when the
    cluster carries `managedBy=elb-dashboard` OR `app=elastic-blast`
    OR (an agent pool named `blastpool` with a `workload=blast` taint).
    Pool name alone is intentionally rejected.
  * `include_unmanaged=True` bypasses the filter — diagnostic escape
    hatch the SPA never sets in normal use.
* Route `GET /api/monitor/aks` made RG-optional. With `resource_group=`
  empty, the route branches to the sub-wide path with cache key
  `("monitor","aks",sub,"sub",include_unmanaged)` (separate tuple
  length from the RG-scoped key so they never collide), wraps in
  `_graceful` so a 403 / RBAC gap renders the empty-state UI, and
  attaches `scope: "subscription"` to the cached payload.
* New `elb-tier` plumb-through:
  * `api.tasks.azure.cluster_params.build_cluster_params(*, ..., tier=None)`
    writes `tags["elb-tier"] = tier.strip()` only when non-empty.
  * `api.tasks.azure.provision.provision_aks(..., tier: str = "")`
    forwards the value.
  * `api.routes.aks.provision` reads `body.get("tier", "")` and forwards
    it through the dispatcher payload.

### Frontend

* `AksClusterSummary` extended with optional `tags`, `tier`,
  `managed_by_elb` fields.
* `monitoringApi.aks(subscriptionId, resourceGroup?)` makes the RG
  arg optional; response type carries optional `scope: "subscription"`.
* `ClusterCard`:
  * `enabled = Boolean(subscriptionId)` (RG no longer required).
  * Query key `["aks", subscriptionId, "sub"]`; query fn omits the RG arg.
  * `recentProvisionAttempt` hydration, save, and cleanup effects
    deleted; the `recentProvisionAttempt.ts` module is removed (no
    other importers).
  * Each `<ClusterItem>` receives `resourceGroup={c.resource_group}`
    so its actions / autoWarmup payloads target the cluster's actual
    RG, not the card's anchor RG.
  * Subtitle: `Subscription-wide · anchor: <rg>` when an anchor RG is
    set, just `Subscription-wide` otherwise.
* `ProvisionModal` gains a `<select id="provision-tier">` populated
  from `CLUSTER_TIER_OPTIONS`. Help text explains the value is written
  to the `elb-tier` ARM tag and editable later in the portal.
* `useClusterProvisioning` exposes `tier` / `setTier`; the provision
  POST body includes `tier` (empty string ⇒ tag is not written).
* `ClusterPulse` / `PulseRowSummary` accept optional `tier` and
  `resourceGroup`; the row renders a small tier pill and the cluster's
  RG next to the name when either is provided.
* `AksProvisionRequest` TypeScript model gains an optional `tier`
  string.

### Storage / IaC

No Bicep change. No new resources, no managed-identity scope change,
no storage public-access flip.

## Validation evidence

* `uv run ruff check api` — clean.
* `uv run pytest -q api/tests/test_monitoring_aks_subwide.py api/tests/test_monitoring_aks_pools.py api/tests/test_smoke.py -k "aks"` — **20 passed**.
* `uv run pytest -q api/tests` — 1454 passed; 1 unrelated pre-existing
  failure (`test_preflight_returns_admission_decision`,
  `validate_blast_database_available` TypeError — verified failing on
  `main` HEAD via `git stash` before this PR's edits).
* `cd web && npx tsc --noEmit` — clean.
* New backend test cases (8) lock the filter contract:
  * `test_subscription_list_filters_unmanaged_by_default` — only the
    tagged cluster surfaces.
  * `test_subscription_list_keeps_legacy_blastpool_with_taint` —
    legacy `blastpool`+`workload=blast` fingerprint still surfaces.
  * `test_subscription_list_rejects_blastpool_name_without_taint` —
    pool name alone is too weak.
  * `test_subscription_list_include_unmanaged_returns_everything` —
    escape hatch flips the filter off.
  * `test_subscription_list_parses_resource_group_from_arm_id` —
    per-row RG correct.
  * `test_subscription_list_surfaces_tier_tag` — `elb-tier` reflected
    on the row.
  * `test_subscription_list_handles_malformed_arm_id` — defensive
    parser returns empty RG.
  * `test_build_cluster_params_writes_tier_only_when_non_empty` —
    blank / missing tier never writes the tag.
* New route test
  `test_smoke.py::test_monitor_aks_subscription_wide_when_rg_missing`
  locks the empty-RG branch + `scope=subscription` envelope.

## Risk notes

* The filter contract is **load-bearing security**: relaxing
  `_is_elb_managed_cluster` would silently start surfacing foreign
  workload clusters on the BLAST controls. The eight tests above are
  the contract guard.
* `include_unmanaged=true` is reserved for an operator-only
  diagnostics chip we intend to add next; the SPA currently never
  sets it.
* The card's anchor RG is no longer the *only* RG the user sees. The
  provision modal still defaults the new cluster's RG to the anchor.
  Multi-RG fleets are an opt-in operator behaviour (provision into a
  different RG via the portal / CLI, or via a future "Provision
  into…" picker we have not built yet).
