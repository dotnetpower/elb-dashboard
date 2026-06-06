# #24 — Extract blast.ts types into blast.types.ts

## Motivation

Issue #24 Priority 2 flags `web/src/api/blast.ts` (1,430 lines) as mixing ~35
type/interface declarations with the runtime API client. This change extracts the
type declarations into a sibling `blast.types.ts` module.

## User-facing change

None. Pure structural refactor — no behaviour, API, or UI change.

## What changed

- New [web/src/api/blast.types.ts](../../../web/src/api/blast.types.ts) holds the
  36 leading `export type` / `export interface` declarations (request/response
  shapes, taxonomy, warmup, citation, recommendation types). Types-only module —
  no runtime values, no side-effect imports.
- [web/src/api/blast.ts](../../../web/src/api/blast.ts) (1,430 → 800 lines) keeps
  the runtime client (`blastApi`, `filenameFromDisposition`,
  `capacityGateBandClass`) and re-exports every type via
  `export type * from "@/api/blast.types"`, so existing
  `import { Foo } from "@/api/blast"` consumers keep working unchanged. The 24
  types the runtime references are also `import type`-ed locally.
- The tail types coupled to `capacityGateBandClass` (`CapacityGateSnapshot`,
  `CapacityGateCounters`, `BlastSubjectAggregate`, `BlastTieCutoff`,
  `BlastTaxonomyRow`) were intentionally left in `blast.ts` next to their only
  runtime consumer — moving them would split a tight type↔function pair for no
  gain.

## Validation evidence

- `cd web && npm run build` — clean (the `export type *` re-export keeps every
  consumer compiling).
- `cd web && npx vitest run` — **706 passed** (no consumer regression).
- `npx eslint src/api/blast.ts src/api/blast.types.ts` — clean.
- `git status` — only `blast.ts` (modified) + `blast.types.ts` (new) touched.

## Remaining #24 work (still deferred)

- `prepare_db.py` `_try_dispatch_aks_mode` full body extraction (HTTPException-
  coupled; needs a domain-error/result-object boundary).
- Component splits `EndpointCard.tsx`, `ProvisionModal.tsx`, `ClusterBento.tsx` —
  these mix UI + state + orchestration and need browser/visual validation, not
  just build + vitest, so they remain separate scoped PRs.
