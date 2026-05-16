# 2026-05-15 — AKS provision UI: system / user pool split + grouped SKU dropdown

## Motivation

The sibling repo (`elastic-blast-azure` commit
`a2d2f0a` — *feat(azure): add default system VM size and configuration for
AKS node pools*) standardised the cluster on a **two-pool layout**:

* `systempool` (mode=System, taint `CriticalAddonsOnly=true:NoSchedule`,
  default VM size `Standard_D2s_v3`) — hosts CoreDNS / metrics-server /
  csi-azuredisk-node etc.
* `blastpool`  (mode=User,   taint `workload=blast:NoSchedule`,
  label `workload=blast`) — runs every ElasticBLAST workload pod.

The dashboard's Celery task and `/api/aks/provision` route were already
wired for this split (see `api/tasks/azure.py::provision_aks` and
`api/services/aks_skus.py`), but the **wizard UI had no controls** for the
system pool — so every cluster was created with the static defaults and
the user could not deviate without hand-editing JSON in the network tab.

Separately, the SKU dropdown was a single flat `<select>` with 31
options. Picking the right SKU for memory-vs-storage workloads required
knowing the naming convention; there was no visual grouping by series.

## User-facing change

* **Create AKS Cluster** modal now has two clearly labelled sections:
  * **Workload pool · `blastpool`** — Node SKU + Node Count (existing).
  * **System pool · `systempool · CriticalAddonsOnly`** — System VM size
    + System node count (1–3, capped to match AKS minimums).
* The cost summary line now breaks down `blastpool` and `systempool`
  separately and sums them, e.g.
  `~$20.26/hr · blastpool: 10 × Standard_E32s_v5 (~$20.16/hr) · systempool: 1 × Standard_D2s_v3 (~$0.10/hr)`.
* Both SKU dropdowns are now grouped by **series** with `<optgroup>`
  separators. Order:
  *System pool · HPC (HB/HC) · Memory-optimised E v5 · Memory + NVMe E bs v5 · Memory E v3 · General D v3 · Storage L v3 · Storage L as v3*.
  Each option also shows the hourly USD price inline:
  `Standard_E32s_v5 (32 vCPUs, 256 GB · $2.02/hr)`.
* The blast pool dropdown only lists SKUs flagged `role=blast|both`; the
  system pool dropdown only lists `role=system|both`. The user can no
  longer accidentally pick a 96-vCPU HPC SKU for the system pool.
* The provisioning banner mirrors the same split:
  `blastpool 10 × Standard_E32s_v5` / `systempool 1 × Standard_D2s_v3 · Est. 5–10 min`.

## API / IaC diff summary

Backend:
* `api/services/aks_skus.py` —
  `SkuListResponse` now carries `group_labels: dict[str,str]` and
  `group_order: list[str]`. `sku_list_response()` builds them from the
  catalog so the SPA never hardcodes the per-series labels.
* `api/tests/test_aks_skus.py` —
  `test_allowed_skus_match_sibling_azure_hpc_machines` now asserts that
  the **blast-only** subset of `SKU_BY_NAME` matches sibling
  `AZURE_HPC_MACHINES`, and that every system-only SKU is allowed and
  contains `DEFAULT_SYSTEM_SKU`. The route test additionally verifies
  `default_system_sku`, every SKU's `role` ∈ {system,blast,both}, and
  that `group_order` covers every used `group` exactly once.

Frontend:
* `web/src/api/aks.ts` — `AksSkuListResponse` gains optional
  `group_labels` + `group_order`.
* `web/src/hooks/useAksSkus.ts` — `FALLBACK_AKS_SKUS` now contains both a
  system-pool entry (`Standard_D2s_v3`) and the blast default with the
  required `role`/`group` fields. New exports
  `DEFAULT_AKS_SYSTEM_SKU`, `groupAksSkus(skus, pool, order, labels)`,
  `defaultSystemSku`, `groupLabels`, `groupOrder`. The fallback group
  labels/order mirror `SKU_GROUP_LABELS` / `SKU_GROUP_ORDER` in the
  backend so the SPA still renders correctly when the API response is
  cached without the new fields. `formatAksSkuOption` now appends the
  hourly USD price.
* `web/src/components/cards/ClusterCard.tsx` — adds
  `systemVmSize` / `systemNodeCount` state, a second SKU `<select>` for
  the system pool, an effect that adopts the backend's
  `default_system_sku` once it loads, and forwards both new fields to
  `aksApi.provision`. Both `<select>`s render `<optgroup>`s sourced from
  `groupAksSkus()`.

No infra changes — the Celery task and `/api/aks/provision` route already
accept `system_vm_size` / `system_node_count`; this PR just wires them
through the UI.

## Validation evidence

* `uv run pytest -q api/tests` — 123 passed.
* `cd web && npm run build` — `tsc -b && vite build` clean
  (671 kB JS gzipped 183 kB; pre-existing chunk-size warning unchanged).
* Manual smoke (next): user will exercise the **+ Add Cluster** modal in
  the dashboard and create a real cluster with the new system / user
  pool fields; the resulting AKS will show two agent pools (`systempool`
  D2s_v3 ×1 + `blastpool` E32s_v5 ×N) per the sibling
  `constants.py::ELB_AZURE_*_POOL_NAME` mirror.

## Cross-repo consistency note

If sibling repo bumps `ELB_DFLT_AZURE_SYSTEM_VM_SIZE`,
`ELB_AZURE_SYSTEM_POOL_NAME`, `ELB_AZURE_BLAST_POOL_NAME`, or the
`workload=blast` label/taint values, update **both** of these in lockstep:

* `api/services/aks_skus.py` (`DEFAULT_SYSTEM_SKU`, `SKU_GROUP_LABELS`),
* `api/tasks/azure.py::provision_aks` (the `SYSTEM_POOL_NAME` /
  `BLAST_POOL_NAME` / `BLAST_TAINT` / `SYSTEM_TAINT` constants).
