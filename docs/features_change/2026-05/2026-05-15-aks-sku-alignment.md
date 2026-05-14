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
- The `/api/aks/skus` dropdown now lists **22** SKUs grouped into 5
  series (`D-v3`, `E-v3`, `E-v5`, `E-v5-bs`, `L-v3`), all guaranteed to
  work with elastic-blast. Five SKUs that used to appear (`D2s_v5`,
  `D4s_v5`, `D8s_v5`, `E4s_v5`, `E8s_v5`) are gone — they would have
  failed at submit time.
- The Cost Estimator dropdown and the Quick-test example were updated
  accordingly (`Standard_D4s_v5` → `Standard_D8s_v3`, etc.).

## Implementation

### New module — single source of truth

- [api/services/aks_skus.py](../../api/services/aks_skus.py) (`ALLOWED_SKUS`,
  `AZURE_VM_HOURLY_USD`, `DEFAULT_SKU = "Standard_E32s_v5"`). Mirrors
  `src/elastic_blast/azure_traits.py::AZURE_HPC_MACHINES` and
  `constants.py::ELB_DFLT_AZURE_MACHINE_TYPE` in the sibling repo, exactly
  the same way [api/services/image_tags.py](../../api/services/image_tags.py)
  mirrors the pinned image tags. A module-level `assert` block self-checks
  that `DEFAULT_SKU` is present in both dicts, so a botched future bump
  fails import instead of failing in production.

### Backend

- [api/routes/stubs.py](../../api/routes/stubs.py): `GET /api/aks/skus` now
  reads from `aks_skus.list_skus()` and returns `default_sku` alongside
  the list. Still flagged `degraded: true` until the Celery task lands —
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
  removed the bogus `Standard_E20s_v5` entry from `SKU_INFO`; updated the
  per-node hourly costs to match the sibling pricing table
  (`AZURE_VM_HOURLY_PRICES` in `azure_traits.py`); added a comment
  pinning the file to the backend allow-list.
- [web/src/pages/tools/ToolTabs.tsx](../../web/src/pages/tools/ToolTabs.tsx):
  hard-coded Cost-Estimator dropdown rebuilt from the allow-list; default
  state SKU `Standard_E16s_v5` → `Standard_E32s_v5`.
- [web/src/pages/BlastSubmit.tsx](../../web/src/pages/BlastSubmit.tsx):
  fallback `machine_type` for a cluster with no `node_sku` flipped from
  `Standard_E16s_v5` to `Standard_E32s_v5`.
- [web/src/data/labToolExamples.ts](../../web/src/data/labToolExamples.ts):
  Quick-test example SKU `Standard_D4s_v5` → `Standard_D8s_v3` (smallest
  allow-listed general-purpose SKU; `MIN_PROCESSORS = 8` in
  `azure_traits.py` would reject anything smaller anyway).

### Sibling repo (`~/dev/elastic-blast-azure`, commit `aebffce7`, **not pushed**)

- `src/elastic_blast/constants.py`: `ELB_DFLT_AZURE_MACHINE_TYPE` flipped
  to `Standard_E32s_v5`.
- `src/elastic_blast/azure.py`: the `apply_optimization_profile()`
  comparison `machine_type != 'Standard_E32s_v3'` now reads
  `ELB_DFLT_AZURE_MACHINE_TYPE` from `constants.py`, so the default and
  the comparison can never drift again.
- `docs/azure-prereq.md`, `docs/azure-pipeline-reference.md`,
  `docs/azure-data-pipeline-analysis.md`: updated examples / tables to
  v5; v3 row in the benchmark table relabelled "baseline (v3)" so the
  measured numbers stay valid.
- `.gitignore`: ignore `.venv/` and `venv/`.

## Validation evidence

- `uv run pytest -q api/tests` → **45 passed** (was 37 — eight new tests
  in [api/tests/test_aks_skus.py](../../api/tests/test_aks_skus.py)
  guard the allow-list, the default constant, and the rejection of the
  five SKUs we just removed).
- Smoke-test of the live route via FastAPI `TestClient`:

  ```
  GET /api/aks/skus  → 200
  default_sku: Standard_E32s_v5
  sku count: 22
  first 3: ['Standard_E16s_v3', 'Standard_E32s_v3', 'Standard_E48s_v3']
  series set: ['D-v3', 'E-v3', 'E-v5', 'E-v5-bs', 'L-v3']
  ```

- `cd web && npm run build` → clean (one informational chunk-size
  warning, unchanged from before).
- Sibling import sanity-check:

  ```
  default: Standard_E32s_v5
  OK: default Standard_E32s_v5 is in allow-list and pricing
  ```

## Follow-ups (out of scope for this change)

1. Replace the `degraded: true` stub for `/api/aks/skus` with a Celery
   task that intersects `ALLOWED_SKUS` with a live
   `Microsoft.Compute/skus` query (per-region availability, quota).
2. Push the sibling commit `aebffce7` to `origin/master` — held back
   pending user OK because pushing affects shared state.
3. Convert the Cost-Estimator dropdown to fetch from `aksApi.listSkus()`
   so there is a single allow-list visible in the SPA. The current
   alignment is correct but still duplicated in TypeScript.
