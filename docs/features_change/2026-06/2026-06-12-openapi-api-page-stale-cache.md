# API page: scope public-HTTPS status by cluster + stop mislabelling 404 as unreachable

## Motivation

Opening the **API** menu right after creating a fresh cluster surfaced a stack
of alarming-looking cards that were really two defects layered on top of the
normal "elb-openapi not deployed yet" state:

1. **Stale public-HTTPS cache leak.** `get_openapi_public_https_status()` read
   the legacy *global* runtime-cache key unconditionally. If any earlier
   cluster had ever enabled public HTTPS, its FQDN was returned for every
   cluster — so a brand-new cluster's API page picked up a dead endpoint,
   drove `baseUrl` truthy, and rendered the spec/deployment cards plus a
   spurious "did not respond → Repair VNet peering" recovery card (which was
   itself a no-op on a shared-VNet topology).
2. **404 mislabelled as unreachable.** The deployment-status card treated any
   `deploymentQuery` error as "workload-cluster unreachable / missing kubectl
   RBAC". A 404 (`openapi_deployment_not_found`) just means elb-openapi is not
   deployed yet — already handled by the service-IP-driven Deploy panel — so a
   fresh cluster was told it had a cluster-access problem it did not have.

## User-facing change

- The API page's public-HTTPS lookup is now scoped to the selected cluster.
  A previously-enabled cluster's URL no longer leaks onto a different cluster,
  so the spec/deployment/"repair peering" cards no longer appear on a fresh
  cluster that simply has no elb-openapi yet. The page collapses to the single
  correct affordance: **Deploy elb-openapi**.
- The "elb-openapi deployment status unavailable" diagnostic no longer appears
  for a 404 (not-deployed) — only for genuine read failures (502 / timeout /
  RBAC).

## API / IaC diff summary

- `GET /api/aks/openapi/public-https` now accepts optional
  `subscription_id` / `resource_group` / `cluster_name` query params. When all
  three are present the lookup uses the per-cluster cache key; otherwise it
  falls back to the legacy global key (backward compatible — the Settings
  panel's call is unchanged).
- `get_openapi_public_https_status(...)` gained matching keyword-only optional
  params; the cluster ARM id is composed only when all three are supplied.
- SPA: `aksApi.openApiPublicHttpsStatus(sub, rg, cluster)` passes the cluster
  context; the React Query key now includes it so switching clusters refetches.
- SPA: `deploymentReadFailed` excludes `status === 404`.

### Root-cause hardening (surfaced by self-critique)

The stale-cache class also affected the **data plane**, not just the API page:
`get_public_tls_base_url()` read the legacy global key unconditionally, so a
cluster that enabled public HTTPS could misroute a *different* cluster's calls
to its FQDN. Worst case: a BLAST submit targeting cluster B routed to cluster
A's endpoint (not masked by any SPA gating).

- `get_public_tls_base_url(*, subscription_id, resource_group, cluster_name)`
  gained keyword-only optional cluster context; composes the per-cluster ARM id
  and reads the per-cluster key. No args → legacy global key (backward
  compatible); `OPENAPI_PUBLIC_BASE_URL` env still wins.
- Threaded cluster context through all three consumers:
  `external_jobs._openapi_client_kwargs_from_cluster` (BLAST submit),
  `aks_openapi_spec`, and `aks_openapi_proxy`.
- SPA Settings `PublicHttpsSection` now scopes its status read to the selected
  cluster.

No Bicep / infra change.

## Validation evidence

- `uv run pytest -q api/tests/test_openapi_public_https.py` → 35 passed
  (`test_public_https_status_scoped_to_cluster` guards the SPA leak;
  `test_public_tls_base_url_scoped_to_cluster` guards the data-plane leak:
  cluster A sees its own URL, fresh cluster B returns empty / `{enabled:false}`,
  env hard-pin still wins, no-context call reads the legacy key).
- `uv run pytest -q api/tests` → 3316 passed, 3 skipped.
- `uv run ruff check` on touched files → clean.
- `cd web && npm run build` → built; `npm test -- --run` → 787 passed.
