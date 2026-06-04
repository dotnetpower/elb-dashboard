# Prepare-DB opts into the fast AKS-fanout azcopy download from the UI

## Motivation

Downloading large BLAST databases (e.g. `core_nt` / `nt`) from the Storage card
was slow even while the AKS workload cluster was running. The backend already
had two download paths:

1. **Server-side copy** — `start_copy_from_url` makes Azure Storage pull each
   blob directly from the NCBI S3 mirror. No `azcopy`, single-stream per blob,
   throttled by NCBI S3. This is the default (`PREPARE_DB_AKS_MODE_DEFAULT=server-side`).
2. **AKS fanout** — `prepare_db_via_aks` spawns parallel `azcopy` pods across
   the AKS nodes. Fast, but only used when the request supplies `mode` plus the
   AKS coordinates.

The SPA's `prepareBlastDb` call only ever sent
`{subscription_id, storage_resource_group, account_name, db_name}` — no `mode`,
no AKS coordinates. So `_try_dispatch_aks_mode` always returned `None` and every
UI-driven download silently fell back to the slow server-side path. The fast
azcopy path was effectively dead for the dashboard.

## User-facing change

The Storage card now opts every BLAST DB download into `mode=auto`:

* The frontend resolves the workload AKS cluster (name + resource group) from
  the subscription-wide cluster list via `pickPreferredCluster(..., { requireNodes: true })`
  and passes the self-consistent `name`/`resource_group` pair to `prepare-db`.
* `mode=auto` makes the backend try the parallel azcopy fanout first and
  transparently fall back to the existing server-side copy when the cluster is
  stopped, has fewer than the required ready nodes, or the kubelet identity
  lacks `Storage Blob Data Contributor`.
* When no cluster can be resolved, the coordinates are omitted and the request
  preserves the legacy server-side-only behaviour — fully backward compatible.

Net effect: when an AKS cluster is available (the common case while warmup is in
progress), large DB downloads now use the fast azcopy fanout instead of the slow
NCBI-S3 server-side copy, with no new button or user action.

## API / IaC diff summary

No backend or IaC changes — this wires the frontend into an already-shipped,
already-tested backend path. Frontend-only:

* `web/src/api/monitoring.ts` — `prepareBlastDb` gains an optional 5th argument
  `aks?: { resourceGroup?: string; clusterName?: string }`. When both are
  present the POST body adds `mode: "auto"`, `aks_resource_group`,
  `cluster_name`; otherwise they are omitted. Response type gains optional
  `mode?: string`.
* `web/src/components/cards/storage/useBlastDb.ts` — `UseBlastDbArgs` gains
  optional `aksClusterName` / `aksResourceGroup`; the `prepareBlastDb` call
  forwards them as a matched pair (only when both are present).
* `web/src/components/cards/storage/BlastDbSection.tsx` — threads the two
  optional props from the card into `useBlastDb`.
* `web/src/components/cards/StorageCard.tsx` — new `aksDownloadCoords` memo
  resolves the preferred cluster's `name`/`resource_group` and passes them to
  `BlastDbSection`.

The AKS download coordinates are kept independent of the existing `clusterName`
prop (which also drives the order-oracle build) so this change does not alter
the oracle build's target cluster.

## Validation evidence

* `cd web && npm run build` — ✓ built in ~10 s, no type errors.
* `cd web && npx vitest run src/components/cards` — 11 files, 78 tests passing.
* `cd web && npx eslint <4 touched files>` — clean.
* `git status --short` / `git diff --stat` — only the four intended frontend
  files changed (+69 / −3).
* Backend contract re-verified against `api/routes/storage/prepare_db.py`:
  `prepare_db` reads `mode` / `aks_resource_group` / `cluster_name` from the
  free-form body; `_try_dispatch_aks_mode` returns `None` (→ server-side) when
  the coordinates are absent and falls back on stopped / under-provisioned /
  missing-RBAC clusters under `mode=auto`.
