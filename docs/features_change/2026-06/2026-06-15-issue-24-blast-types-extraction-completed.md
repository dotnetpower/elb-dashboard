---
title: "Issue #24 — extract remaining blast.ts types to blast.types.ts"
description: "Move the five type definitions still inline in web/src/api/blast.ts (CapacityGate*, BlastSubjectAggregate, BlastTieCutoff, BlastTaxonomyRow) into blast.types.ts so the API client module carries only the blastApi client + one pure helper."
tags:
  - contributor
  - blast
---

# Issue #24 — `blast.ts` type extraction completed

## Motivation

Issue #24 (split oversized files violating SRP) lists `web/src/api/blast.ts`
under Priority 2 with the explicit instruction "extract types to
`blast.types.ts`". A prior pass had already moved most types and added a
`export type * from "@/api/blast.types"` back-compat re-export, but **five type
definitions were still declared inline** in `blast.ts` after the `blastApi`
const:

- `CapacityGateSnapshot`
- `CapacityGateCounters`
- `BlastSubjectAggregate`
- `BlastTieCutoff`
- `BlastTaxonomyRow`

This left `blast.ts` mixing the API client object with type declarations.

## User-facing change

None. Pure structural refactor. No runtime behaviour, no API contract change.

## Diff summary

- **`web/src/api/blast.types.ts`** (696 → 790 lines): the five interfaces/types
  moved here verbatim (comments preserved).
- **`web/src/api/blast.ts`** (811 → 718 lines): the five type declarations
  removed; the pure helper `capacityGateBandClass` stays (it is logic, not a
  type). The types the `blastApi` client and the helper reference
  (`CapacityGateSnapshot`, `BlastSubjectAggregate`, `BlastTieCutoff`,
  `BlastTaxonomyRow`) are now imported from `blast.types.ts`.
- **Back-compat:** the existing `export type * from "@/api/blast.types"` in
  `blast.ts` re-exports all five moved types, and `web/src/api/endpoints.ts`
  does `export * from "@/api/blast"`, so every consumer importing these via
  `@/api/blast` or `@/api/endpoints` (e.g. `CapacityGateCell.tsx`,
  `DescriptionsTabBody.tsx`, `TaxonomyPanel.tsx`) keeps working unchanged.

`blast.ts` is now the `blastApi` client object plus one pure helper — a single
responsibility (the BLAST API client). All BLAST types live in `blast.types.ts`.

## Validation evidence

- `cd web && npm run build` → built (tsc -b clean; pre-existing chunk-size
  warning only).
- `npx eslint src/api/blast.ts src/api/blast.types.ts` → clean (exit 0).
- `npx vitest run src/components/cards/ClusterBento/CapacityGateCell.test.ts` →
  7 passed; `npx vitest run src/pages/blastResults` → 134 passed (12 files) —
  covers all consumers of the moved symbols.

## Remaining issue #24 work (still open)

- SettingsPanel sections over ~600 lines: `TelemetrySection` (626),
  `PublicHttpsSection` (663), `DiagnosticsSection` (745), `VnetPeeringSection`
  (749).
- `web/src/components/cards/ClusterBento/ClusterBento.tsx` (948) not yet split.
