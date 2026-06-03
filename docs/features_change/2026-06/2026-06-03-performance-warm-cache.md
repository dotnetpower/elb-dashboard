# Settings → Performance: per-cluster warm-cache mode

## Motivation

Every time an AKS cluster starts (`az aks stop` → `start`), the BLAST database
warmup re-runs from scratch. `az aks stop` deallocates the VMSS instances, so the
node RAM page cache is always lost and **ephemeral OS disks are wiped**, forcing a
full re-download + re-stage of the database shards on each restart. This is the
single largest contributor to cold-start latency and repeated egress/compute cost.

The disk that backs the staged database is chosen at **cluster CREATE time** and
cannot be changed on a running cluster, so the only durable fix is to let an
operator opt a cluster's *next* provision into a persistent-disk layout — while
keeping today's zero-cost ephemeral behaviour as the default.

## User-facing change

New **Settings → Performance** section (per-cluster scope):

- Discovers clusters subscription-wide and shows a radio group with three
  warm-cache modes:
  - **Ephemeral (current)** — default; re-download on every start. Byte-identical
    to historical behaviour. Lowest cost.
  - **Node OS disk** — pins the blastpool to a Managed 512 GiB OS disk so the
    staged database survives node recycling on the next-provisioned cluster.
  - **Dedicated data disk (preview)** — tags the cluster for a dedicated managed
    data disk (PVC); the dedicated-disk warmup path is rolling out and currently
    falls back to ephemeral staging.
- A `StatusLine` makes clear the choice **applies to the next cluster provision**,
  not the running cluster (OS disk type is fixed at create time).
- The currently-saved mode is marked with a "Current" badge; Save is disabled
  until the selection differs from the saved value.

## API / IaC diff summary

New backend service + route (no IaC change):

- `api/services/performance_pref.py` — per-cluster `PerformancePreference`
  persistence. Azure Table backend (`performancepref`) when
  `AZURE_TABLE_ENDPOINT` + `CONTAINER_APP_NAME` are both set; JSON-file fallback
  (`ELB_LOCAL_STATE_DIR`) for local dev. `resolve_warm_cache_mode()` returns the
  default `ephemeral` when no row exists; invalid stored modes normalise to
  `ephemeral`.
- `api/routes/settings/performance.py` — `GET /api/settings/performance`
  (query `subscription_id`/`resource_group`/`cluster_name`; never 404, defaults
  `ephemeral`) and `PUT /api/settings/performance` (validated body → save). Both
  `Depends(require_caller)`; identifiers validated with strict regexes →
  `HTTPException(400)` on bad input; invalid mode → `422` (Pydantic `Literal`).
- `api/routes/settings/__init__.py` — mounts the router under
  `/settings/performance`.
- `api/tasks/azure/cluster_params.py` — new `warm_cache_mode` param.
  `node_disk` → blastpool `os_disk_type="Managed"`, `os_disk_size_gb=512` and an
  `elb-warm-cache=node_disk` cluster tag. `data_disk` → tag only (PVC realised in
  the warmup task). `ephemeral` → no disk fields, no tag — **byte-identical** to
  the previous payload (the SDK treats an omitted kwarg as `None`).
- `api/tasks/azure/provision.py` — resolves the saved mode via
  `resolve_warm_cache_mode(...)` and threads it into `build_cluster_params(...)`.

Frontend:

- `web/src/api/settings.ts` — `WarmCacheMode` type, request/response models,
  `getPerformance` / `putPerformance`.
- `web/src/components/SettingsPanel.tsx` — `PerformanceSection` (cluster discovery,
  radio group, dirty-check Save, "applies to next provision" note).

## Validation evidence

- `uv run pytest -q api/tests/test_performance_pref.py
  api/tests/test_settings_performance.py` → **11 passed** (round-trip,
  missing→None, default ephemeral, invalid→ephemeral, identity-field validation,
  list).
- `uv run pytest -q api/tests/test_azure_provision_aks.py` → **38 passed**,
  including the new byte-identical regression guard
  (`test_build_cluster_params_default_warm_cache_is_byte_identical`),
  `…_node_disk_pins_managed_os_disk`, and
  `…_data_disk_tags_but_keeps_default_disk`.
- Full suite `uv run pytest -q api/tests` → **2478 passed, 3 skipped** (one
  unrelated parallel-load flake in `test_terminal_exec.py::
  test_run_truncates_stdout_above_cap` that passes in isolation).
- `uv run ruff check api` → all checks passed.
- `cd web && npm run build` → built clean, no TypeScript errors.

## Follow-up

- PR4: realise the `data_disk` PVC path in the terminal-sidecar warmup
  (`terminal/patch_elastic_blast.py` + K8s templates) with a Premium SSD
  StorageClass and graceful fallback to ephemeral on PVC attach failure. Requires
  live-cluster validation, hence kept as a separate staged change. The UI labels
  the mode "(preview)" until then.

## Multi-cluster notes / known limitations

- **Per-cluster isolation**: the preference is keyed by
  `(subscription_id, resource_group, cluster_name)`; each `provision_aks` resolves
  its own cluster's mode, so there is no cross-cluster leakage. A missing row
  reads back as `ephemeral` (byte-identical default).
- **node_disk effectiveness**: the staged DB lives on the node-local
  `hostPath: /workspace`, which is on the node **root filesystem (OS disk)** — not
  the temp/resource disk. Pinning a Managed OS disk therefore preserves
  `/workspace` (and the `.download-complete` marker) across an `az aks stop`/
  `start` deallocate cycle. It does **not** survive a full node *replacement*
  (new VMSS instance gets a fresh OS disk) — that is what `data_disk` (PVC) is
  for. The UI copy reflects this distinction.
- **Observability**: `provision_aks` now logs an INFO line when a cluster is
  provisioned with a non-default `warm_cache_mode`, so the choice is visible in
  App Insights. The default `ephemeral` path stays silent.
- **Known limitation (fail-safe)**: the SPA resolves the selected cluster's RG by
  *name* (`availableClusters.find(c => c.name === clusterName)`), the same pattern
  the Observability / Cluster sections use. If two clusters in one subscription
  share a name across different resource groups, the UI may resolve the wrong RG
  and write the preference under that RG's key. This **fails safe**: `provision_aks`
  reads the preference by the cluster's *real* `(sub, rg, name)`, so a
  mis-keyed row is simply not found and the cluster provisions with the default
  `ephemeral` behaviour — never the wrong mode on the wrong cluster. A shared
  refactor to carry the RG in the dropdown value (across all five Settings
  sections) is the durable fix and is tracked as a separate follow-up.

