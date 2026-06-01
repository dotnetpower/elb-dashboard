---
title: Runtime summary reflects the effective warmup / sharding that will run
description: Fixes the BLAST New Search Runtime summary showing Warmup and Sharding as "off" for an already-warm, shard-ready database even though the queued run uses the warm cache and sharded mode.
tags:
  - blast
  - ui
  - aks
---

# Runtime summary reflects the effective warmup / sharding that will run

## Motivation

Even after the [sharded-throughput warmup-status conflation fix](2026-06-01-sharded-throughput-warmup-status-conflation.md),
the right-rail **Runtime summary** on the New Search screen still showed
**Warmup: off** and **Sharding: off** on a deployed cluster (`elb-cluster-02`)
where `core_nt` was warm and shard-ready.

Root cause was a **display-vs-submit divergence**, not a backend problem. The
deployed `/api/monitor/aks/warmup-status` returned `warm: true` for `core_nt`
(`status: "Ready"`, `sources: ["warmup"]`, `progress_pct: 100`, `nodes_ready: 10`)
and the cluster list serialized `resource_group` / `power_state` correctly. The
submit payload already derives the real run from **effective** values
(`effectiveShardingMode`, and the already-warm short-circuit), so the actual
queued run was sharded and warm. But `SubmitSummaryRail` rendered the **raw**
`form.sharding_mode` and `form.enable_warmup`, which only flip on once the
`reconcileShardingSelection` effect has mutated `form`. While runtime data /
warmup-status is still resolving — or in any transient window before the effect
fires — the summary reported "off" while a sharded, warm run was in fact about to
be submitted. The summary was lying about what would run.

## User-facing change

- The Runtime summary now shows the **effective** sharding mode (the same
  `effectiveShardingMode` the submit payload uses), so it reads `precise` /
  `approximate` instead of `off` once the cluster + warm + capacity state make a
  sharded run the queued behaviour.
- The Runtime summary's Warmup row now reads **"warm cache ready"** when the
  selected database is already warm on the selected cluster, instead of "off"
  while `form.enable_warmup` lags. It still reads `enabled` when warmup is
  requested for a cold DB, and `off` only when neither warm nor requested.
- No change to what is actually submitted — the submit payload already used the
  effective values. This makes the summary truthful, independent of the reconcile
  effect's timing.

## Code change summary

- `web/src/pages/blastSubmit/runtimeSummaryDisplay.ts` (new): pure
  `runtimeWarmupDisplay` / `runtimeShardingDisplay` helpers that translate the
  effective submit values into the rail's short labels.
- `web/src/pages/blastSubmit/SubmitSummaryRail.tsx`: accepts optional
  `effectiveShardingMode` + `isDbAlreadyWarm` props and renders the helper
  output. Props default to the raw form values when omitted (backward
  compatible).
- `web/src/pages/BlastSubmit.tsx`: passes `effectiveShardingMode` and
  `isDbAlreadyWarm` (already computed for the submit payload) into the rail.
- No API or IaC change.

## Validation

- `web/src/pages/blastSubmit/runtimeSummaryDisplay.test.ts` (new, 5 cases):
  asserts the warm-cache short-circuit and effective-over-raw sharding
  preference, including the exact reported scenario (`isDbAlreadyWarm: true,
  enableWarmup: false` → "warm cache ready"; `effectiveShardingMode: "precise",
  formShardingMode: "off"` → "precise").
- Full web suite green: `cd web && npm test -- --run` → 468 passed (60 files).
- `cd web && npm run build` → clean.
- ESLint clean on the touched files.
- Live deployed backend re-confirmed during investigation:
  `GET /api/monitor/aks/warmup-status?...cluster_name=elb-cluster-02` →
  `warm: true`, `core_nt {status: "Ready", sources: ["warmup"],
  progress_pct: 100}`; cluster list serialized `resource_group: rg-elb-cluster`,
  `power_state: Running`.
