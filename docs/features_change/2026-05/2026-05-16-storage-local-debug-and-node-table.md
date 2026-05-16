# 2026-05-16 — Storage local-debug button + AKS node table polish

## Motivation

Two follow-ups from the v3 dashboard redesign rolled out on the same day:

1. **BLAST DB card silently rendered "0/9" with no explanation.** Storage was
   private-only as designed, but `BlastDbSection` wasn't aware of the
   degraded `network_blocked` mode emitted by the data-plane probe. Operators
   had no signal that the empty count was caused by network gating, not by a
   missing database.
2. **`scripts/dev/storage-public-access.sh on/off` was the only escape hatch
   for local development**, and it was not discoverable from the dashboard.
   Putting a "flip public access" button in the always-on UI is exactly what
   §9 of `.github/copilot-instructions.md` forbids — but a *local-only*
   button is acceptable because it is gated by the absence of the
   `CONTAINER_APP_NAME` env var (which is always set when the api sidecar
   runs inside a Container App).

The same revision swept the AKS node-resources table for nine cosmetic and
informational issues called out in the post-redesign critique.

## User-facing change

### BLAST DB card

* New **amber banner** appears on the BLAST DB section when the data-plane
  probe is blocked because the storage account is set to
  `publicNetworkAccess: Disabled`. The banner explains the situation and,
  when the api sidecar is running locally, exposes an
  **`Enable for local debug`** button that calls
  `POST /api/storage/local-debug/open` to invoke the same logic as
  `scripts/dev/storage-public-access.sh on` (publicNetworkAccess=Enabled,
  defaultAction=Deny, IP-allowlist with the api sidecar's caller IP).
* The header `9 ready` chip is replaced with an amber `🔒 blocked` chip when
  the probe is blocked, so the visual cue matches the body explanation.
* The same `Enable for local debug` button appears in the BlastDbModal warning
  strip when `canEnableLocalAccess` is true.
* The button is **never rendered when `CONTAINER_APP_NAME` is set**, so the
  attack surface remains zero in production.

### AKS Node Resources table

* Each row gets a **4 px pool-color stripe** on the left (warning/orange for
  system pools, accent/blue for user pools).
* Rows are now **grouped by pool** with a `System · aks-systempool · 1 node`
  / `User · aks-blastpool · 2 nodes` section header — mirrors the pool cards
  above so operators can correlate at a glance.
* Resource units are humanized: `0.20 / 4 cores` and `0.5 / 7.4 GiB`, with
  the raw millicores / KiB kept in the row's `title` tooltip for power
  users.
* Cluster total summary in the header: `3 nodes · 0.30 / 12 cores (2%) ·
  1.5 / 22 GiB (7%)`, plus a red `· 1 NotReady` segment when applicable.
* Each row name is preceded by a small **Ready dot** (green/red), and a
  `MemoryPressure` / `DiskPressure` chip appears next to the node name if
  the node reports any pressure condition.
* Bars now have a 4 px minimum width (instead of 2 %) so 0 % usage is still
  visible, and each bar's `title` shows the raw `m` / `Mi` numbers.

### Polish

* **Storage card → Public cell** carries an explanatory tooltip; the HNS
  cell is now neutral (it is a config choice, not a "good/bad" signal).
* **Storage card → Containers table** renders the access value `None` as
  `Private` (with the existing lock icon), since `None` reads like an error
  to non-experts. Container `last_modified_time` is now shown as a relative
  string (`2h ago`, `3d ago`) with the absolute timestamp in the `title`.
* **Cluster card → State** is now a `dv3-pill` chip (`Succeeded` →
  `dv3-pill-success`, `Creating`/`Updating` → accent + spinner,
  `Deleting` → warning + spinner, `Failed` → danger).
* **Cluster card → Kubelet OID** moved out of the always-visible card body
  into the "Identity" panel inside the `View full details` modal, with a
  copy button and a one-line note about AcrPull.
* **Terminal card** wording: `ttyd loopback 127.0.0.1:7681 · upstream 200`
  is now `Listening on 127.0.0.1:7681 · last probe HTTP 200`.

## API / IaC diff summary

### Backend (`api/`)

| File | Change |
|------|--------|
| `api/services/storage_public_access.py` | Added `is_running_locally()` (returns `True` when `CONTAINER_APP_NAME` is unset). `ensure_local_storage_access(*, force=False)` now bypasses the `LOCAL_DEBUG_AUTO_OPEN_STORAGE` env-var gate when `force=True` (button click) but still refuses with `{"action": "noop", "reason": "running inside a Container App; refusing to flip public access"}` when invoked from inside a Container App. New `read_local_storage_state(...)` returns the read-only view used by the dashboard. |
| `api/services/storage_data.py` | `classify_storage_failure` now sets `public_access_disabled: True` alongside `degraded_reason: "network_blocked"` so the SPA can detect the disabled state without parsing strings. |
| `api/routes/storage.py` | New `GET /api/storage/local-debug` (always 200; returns `{is_local: false}` when deployed). New `POST /api/storage/local-debug/open` (403 when deployed; otherwise calls `ensure_local_storage_access(force=True)`). |
| `api/services/k8s_monitoring.py` | `k8s_top_nodes` enriched with `cpu_m`, `mem_ki`, `cpu_capacity_m`, `mem_capacity_ki`, `pool`, `ready`, `conditions`. Single `/api/v1/nodes` GET (`_node_capacity_with_meta`) now feeds capacity + metadata. |

### Frontend (`web/src/`)

| File | Change |
|------|--------|
| `web/src/api/monitoring.ts` | `K8sNodeMetrics` interface gained the seven new optional fields backend now emits. |
| `web/src/api/storage.ts` | New typed client for the two `/api/storage/local-debug*` routes. |
| `web/src/api/endpoints.ts` | Re-exports `@/api/storage`. |
| `web/src/components/cards/storage/useBlastDb.ts` | Detects `publicAccessDisabled` from either `degraded_reason` or the new `public_access_disabled` flag. Added `localDebugQuery` (30 s polling when blocked) and `enableLocalAccess()` returning a toast-friendly `{ok, message}` payload. |
| `web/src/components/cards/storage/BlastDbSection.tsx` | New amber banner + `Enable for local debug` button. |
| `web/src/components/cards/storage/BlastDbModal.tsx` | Same Enable button mirrored inside the modal warning strip. |
| `web/src/components/cards/storage/StorageMetaGrid.tsx` | HNS cell neutral; Public cell carries explanatory tooltip. |
| `web/src/components/cards/storage/StorageContainersTable.tsx` | Humanized access labels (`Private`/`Public (blob)`) and relative timestamps. |
| `web/src/components/cards/TerminalCard.tsx` | Friendlier wording. |
| `web/src/components/ClusterDiagnostics.tsx` | `NodeResourcesSection` rewritten — pool grouping, color stripe, humanized units, Ready dot, pressure chip, cluster totals. |
| `web/src/components/ClusterItem.tsx` | `State` is now a `dv3-pill` chip; Kubelet OID line removed from card body. |
| `web/src/components/ClusterDetailModal.tsx` | New `Identity` panel with Kubelet OID + copy button + AcrPull note. New `kubeletObjectId` prop. |

### Infra

No Bicep / azd changes. The local-debug endpoints are gated entirely at
runtime by the absence of `CONTAINER_APP_NAME`; the deployed Container App
sets that env var via the platform.

## Validation evidence

* `uv run pytest -q api/tests` → **219 passed in 20.39 s** (incl. 16
  passing `test_storage_public_access` cases).
* `cd web && npx tsc --noEmit` → clean.
* `cd web && npm run build` → clean (671 kB JS / 89 kB CSS, same chunking
  warning as the prior build).
* Local stack restart (`docker compose ... restart api`) followed by
  `curl 127.0.0.1:18080/api/health` → `{"status":"ok",...}`.
* Visual verification at `http://127.0.0.1:18080/`: BLAST DB section now
  renders the amber banner with the `Enable for local debug` button when
  the storage account is `publicNetworkAccess: Disabled`; node table groups
  rows by pool and shows the `0.20 / 4 cores` style readout.

## Security checklist

* No secrets, no SAS issuance, no relaxation of `publicNetworkAccess`
  in production paths. The local-debug button refuses with HTTP 403 and the
  POST handler additionally returns `noop / reason: running inside a
  Container App` if the env-var guard is somehow bypassed.
* Both new endpoints validate inputs through the existing regex guards
  (`_RE_SUB`, `_RE_RG`, `_RE_STORAGE_ACCOUNT`).
* MSAL bearer validation via the existing dependency on `/api/storage/*`
  remains in force.
