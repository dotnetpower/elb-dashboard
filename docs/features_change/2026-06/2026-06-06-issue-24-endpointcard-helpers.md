# #24 — Extract EndpointCard response helpers

## Motivation

Issue #24 Priority 2 flags `web/src/pages/apiReference/EndpointCard.tsx` (1,093
lines) as mixing card render + request builder + response viewer + try-it
execution. As a low-risk first slice (verifiable with build + unit tests, no
visual validation needed), the seven stateless response-formatting helpers at
the bottom of the file are extracted into a sibling module.

## User-facing change

None. Pure structural refactor — the helpers moved verbatim, same behaviour.

## What changed

- New [web/src/pages/apiReference/endpointResponseHelpers.ts](../../../web/src/pages/apiReference/endpointResponseHelpers.ts)
  holds `safeParseJson`, `sortResponses`, `getPathIdHint`, `responseTitle`,
  `responseTone`, `responseBackground`, `responseBorder`, plus the
  `ResponseEntry` tuple type. Pure functions — no React, no state, no fetches.
- [EndpointCard.tsx](../../../web/src/pages/apiReference/EndpointCard.tsx) imports
  them and drops the now-redundant local `ResponseEntry` type. The big card
  component (render / try-it / response viewer) is unchanged.
- New `endpointResponseHelpers.test.ts` (6 cases) pins the parse/sort/colour
  logic, including the "sort does not mutate input" and `{job_id}` hint contracts.

## Validation evidence

- `cd web && npm run build` — clean.
- `cd web && npx vitest run` — **712 passed** (706 prior + 6 new; no regression).
- `npx eslint …EndpointCard.tsx …endpointResponseHelpers.ts(.test.ts)` — clean.

## Remaining #24 work (still deferred)

- The `EndpointCard` component body itself (request builder / try-it executor)
  and the other component splits (`ProvisionModal.tsx`, `ClusterBento.tsx`) mix
  UI + state + orchestration and need browser/visual validation, so each stays a
  separate scoped PR.
- `prepare_db.py` `_try_dispatch_aks_mode` full body extraction.
