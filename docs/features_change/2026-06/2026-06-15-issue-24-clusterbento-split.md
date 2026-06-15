---
title: "Issue #24 — split ClusterBento.tsx into presentation + data hook + readiness panel"
description: "Reduce web/src/components/cards/ClusterBento/ClusterBento.tsx from 948 to 584 lines by extracting the data-orchestration into a useClusterBentoModel hook and the not-ready fallback into a ClusterReadinessBento module, leaving ClusterBento.tsx as the live bento presentation only."
tags:
  - contributor
  - ui
---

# Issue #24 — `ClusterBento.tsx` split into focused modules

## Motivation

Issue #24 (split oversized files violating SRP) lists
`web/src/components/cards/ClusterBento/ClusterBento.tsx` (948 lines) under
Priority 2 as "Multiple sub-panels rendered in one file." The single file mixed
three responsibilities: data orchestration (4 upstream queries + ~25 derived
view models), the live "Mission Control Bento" grid presentation, and a separate
not-ready fallback panel.

## User-facing change

None. Pure structural refactor — the live render body is byte-identical and the
readiness panel markup is unchanged. No behaviour, no API contract change.

## Diff summary

- **New `useClusterBentoModel.ts`** (263 lines): owns all data orchestration —
  `useNodeSummary`, the scoped BLAST jobs query, `/api/blast` request metrics,
  AKS events — plus every derived view model (submit window/timeline, active-job
  rows, API latency/error tones, peak user-pool CPU/mem, overall cluster health
  verdict). Returns a typed model object. The shared `SUBMIT_SPARK_WINDOW_MIN`
  constant is exported from here.
- **New `ClusterReadinessBento.tsx`** (218 lines): the not-ready fallback
  (`ClusterReadinessBento` + its private `ReadinessPill`) rendered when the
  cluster is starting / stopping / stopped / provisioning. No data hooks — a
  distinct responsibility from the live grid.
- **`ClusterBento.tsx`** (948 → 584 lines): now owns presentation only. It calls
  `useClusterBentoModel(...)`, destructures the model, and renders the bento grid
  (live render body unchanged). The small presentational helpers `DegradedHint`
  and `EmptySubmitState` stay here (they are local to the live render). Removed
  the now-unused imports (`useMemo`, `useQuery`, `blastApi`/`monitoringApi`,
  `useNodeSummary`, the job/event/submit mapping helpers, `ClusterHealth` type,
  `Loader2`, `getAksProvisioningLabel`, `emptyNodeSummary`, …) and the constants
  that moved to the hook.

No external consumer imported any of the moved symbols (all were file-local),
so no other file changed. `index.ts` still exports `ClusterBento` unchanged.

## Validation evidence

- `cd web && npm run build` → built (tsc -b clean; pre-existing chunk-size
  warning only).
- `npx eslint` on the three files → clean (exit 0); no unused-import survivors.
- `npx vitest run src/components/cards/ClusterBento` → **41 passed (4 files)**
  (CapacityGateCell, eventMapping, jobMapping, submitMetrics — the existing
  coverage for the helpers the split relies on).

## Remaining issue #24 work (still open)

- SettingsPanel sections over ~600 lines: `TelemetrySection` (626),
  `PublicHttpsSection` (663), `DiagnosticsSection` (745), `VnetPeeringSection`
  (749).
