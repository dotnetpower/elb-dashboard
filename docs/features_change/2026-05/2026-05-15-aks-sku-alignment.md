# 2026-05-15 — AKS SKU allow-list aligned with sibling repo

## Motivation

The dashboard was exposing AKS node SKUs that the sibling
`elastic-blast-azure` CLI does **not** accept. ElasticBLAST raises

```
NotImplementedError: Cannot get properties for <sku>
```

(`elastic_blast.azure_traits.get_machine_properties`) for any SKU outside
its `AZURE_HPC_MACHINES` allow-list, so picking one of the bad options in
the SPA dropdown made BLAST submit fail late, in the cluster, with no
useful UI feedback.

The dashboard also defaulted job templates to `Standard_D8s_v3` while the
sibling default is `Standard_E32s_v*` — researchers got an
under-provisioned 8-vCPU/32-GiB node when they didn't override the SKU.

## User-facing change

- **Default cluster SKU is now `Standard_E32s_v5`** (32 vCPU, 256 GiB RAM,
  Ice Lake). Same shape as the previous v3 default, with newer CPUs and
  higher network bandwidth. Existing AKS clusters keep whatever SKU they
  were created with — only newly-submitted jobs / newly-created clusters
  see the change.
- The `/api/aks/skus` dropdown now lists **31** SKUs from the sibling
  `AZURE_HPC_MACHINES` allow-list. Newly surfaced options include the
  sibling-supported H-series HPC nodes, `Standard_E64is_v3`, larger
  Intel L-series nodes (`L48/L80`), and AMD L-series nodes (`L*as_v3`).
  `Standard_E16s_v3`, `Standard_E32s_v3`, and `Standard_E48s_v3` were
  removed because they are not in the sibling allow-list.
- The price table now mirrors sibling `AZURE_VM_HOURLY_PRICES` for every
  allowed SKU; `Standard_E48bs_v5` was corrected from `3.648` to `3.576`.
- The Cost Estimator dropdown and the Quick-test example were updated
  accordingly (`Standard_D4s_v5` → `Standard_D8s_v3`, etc.).

## Implementation

### SKU catalog - single source of truth

- [api/services/aks_skus.py](../../api/services/aks_skus.py) now defines one
  `SKU_CATALOG` of `SkuCatalogEntry` rows. `ALLOWED_SKUS`,
  `AZURE_VM_HOURLY_USD`, `list_skus()`, and the `/api/aks/skus` payload are
  derived from that catalog, so the allow-list and pricing table cannot drift
  independently. The catalog still mirrors
  `src/elastic_blast/azure_traits.py::AZURE_HPC_MACHINES`,
  `AZURE_VM_HOURLY_PRICES`, and
  `constants.py::ELB_DFLT_AZURE_MACHINE_TYPE` in the sibling repo.
- `SkuSpec` now includes `hourlyUsd`; the frontend no longer needs its own
  small duplicate pricing table for cluster cost hints.

### Backend

- [api/routes/stubs.py](../../api/routes/stubs.py): `GET /api/aks/skus` now
  delegates response construction to `aks_skus.sku_list_response()` and
  returns both `default` and `default_sku` alongside the list, so old and new
  typed clients agree on the default field. Still flagged `degraded: true`
  until the Celery task lands —
  that task will intersect this allow-list with a live
  `Microsoft.Compute/skus` query for region availability + quota.
- [api/services/blast_config.py](../../api/services/blast_config.py):
  - `generate_config()` default for `cluster.machine-type` flipped from
    `Standard_D8s_v3` to `aks_skus.DEFAULT_SKU`.
  - `AZURE_VM_HOURLY_USD` is now a re-export of `aks_skus.AZURE_VM_HOURLY_USD`
    (kept as a module-level name so legacy `legacy/functionapp/routes/blast_tools.py`
    keeps working without edits).

### Frontend

- [web/src/components/cards/ClusterCard.tsx](../../web/src/components/cards/ClusterCard.tsx):
  removed the local `SKU_INFO` pricing table and now uses the shared SKU hook
  for dropdown options, labels, descriptions, and per-node hourly cost hints.
- [web/src/pages/tools/ToolTabs.tsx](../../web/src/pages/tools/ToolTabs.tsx):
  Cost-Estimator dropdown now uses the same shared SKU hook instead of
  duplicating a stale hard-coded subset; default state remains
  `Standard_E32s_v5`.
- [web/src/hooks/useAksSkus.ts](../../web/src/hooks/useAksSkus.ts): new
  shared hook for SKU fetching, fallback data, option labels, and short SKU
  descriptions.
- [web/src/api/endpoints.ts](../../web/src/api/endpoints.ts): typed
  `/api/aks/skus` response now includes `default_sku`, `degraded`,
  `degraded_reason`, and catalog-derived `hourlyUsd`.
- [web/src/pages/BlastSubmit.tsx](../../web/src/pages/BlastSubmit.tsx):
  fallback `machine_type` for a cluster with no `node_sku` flipped from
  `Standard_E16s_v5` to `Standard_E32s_v5`.
- [web/src/data/labToolExamples.ts](../../web/src/data/labToolExamples.ts):
  Quick-test example SKU `Standard_D4s_v5` → `Standard_D8s_v3` (smallest
  allow-listed general-purpose SKU; `MIN_PROCESSORS = 8` in
  `azure_traits.py` would reject anything smaller anyway). The production
  example now uses the default `Standard_E32s_v5` instead of `E16s_v5`.
- Removed unused `web/src/pages/remoteTerminalModel.ts`, a retired Remote
  Terminal VM helper that still carried unsupported VM-size choices. The
  browser terminal is a sidecar now, so this model should not remain in the
  active frontend tree.

### Sibling repo reference (read-only for this dashboard change)

- `~/dev/elastic-blast-azure/src/elastic_blast/constants.py` still sets
  `ELB_DFLT_AZURE_MACHINE_TYPE = 'Standard_E32s_v5'`.
- `~/dev/elastic-blast-azure/src/elastic_blast/azure_traits.py` currently
  exposes 31 entries in `AZURE_HPC_MACHINES` and matching prices in
  `AZURE_VM_HOURLY_PRICES`; this dashboard mirror now matches both sets.

## Validation evidence

- `uv run pytest -q api/tests/test_aks_skus.py` → **12 passed**. The test
  file now guards the exact sibling SKU set, default constant, full pricing
  coverage, representative sibling price values, and the `/api/aks/skus`
  `default` / `default_sku` / `hourlyUsd` response contract.
- Direct AST comparison against the sibling source files:

  ```
  sibling_skus=31 current_skus=31
  missing=[]
  extra=[]
  price_mismatch=[]
  ```

- `uv run pytest -q api/tests` → **71 passed**.
- Smoke-test of the live route via FastAPI `TestClient`:

  ```
  status=200
  default_sku=Standard_E32s_v5
  sku_count=31
  has_hpc=True
  has_l_as=True
  ```

- `cd web && npm run build` → clean (one informational chunk-size
  warning, unchanged from before).
- `cd web && npx eslint src/api/endpoints.ts src/hooks/useAksSkus.ts src/components/cards/ClusterCard.tsx src/pages/tools/ToolTabs.tsx src/data/labToolExamples.ts --max-warnings 0`
  → clean.
- Active-code stale SKU search now only finds unsupported SKUs inside the
  negative regression test in [api/tests/test_aks_skus.py](../../api/tests/test_aks_skus.py).
- Sibling import sanity-check:

  ```
  default: Standard_E32s_v5
  OK: default Standard_E32s_v5 is in allow-list and pricing
  ```

## Follow-ups (out of scope for this change)

1. Replace the `degraded: true` stub for `/api/aks/skus` with a Celery
   task that intersects `ALLOWED_SKUS` with a live
   `Microsoft.Compute/skus` query (per-region availability, quota).
