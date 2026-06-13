---
title: Split EndpointCard.tsx into focused sub-components
description: >-
  The API Reference EndpointCard was split into EndpointResponsesDoc and
  EndpointTryItPanel sub-components, dropping the card shell from 977 to 388
  lines with byte-identical render output.
tags:
  - ui
  - contributor
---

# Split EndpointCard.tsx into focused sub-components

## Motivation

Issue [#24](https://github.com/dotnetpower/elb-dashboard/issues/24) Priority 2
flags `web/src/pages/apiReference/EndpointCard.tsx` (977 lines) as mixing the
card shell, the read-only "Responses" documentation, and the interactive
"Try it" request panel in one component. The recorded #24 follow-up note
(`2026-06-06-issue-24-blast-types-extraction.md`) deferred the component splits
as "separate scoped PRs needing browser/visual validation". This is that scoped
PR for `EndpointCard`.

## User-facing change

None. Pure structural refactor — the JSX (elements, inline styles, props) was
relocated verbatim, so the rendered DOM tree is byte-identical.

## What changed

- New [web/src/pages/apiReference/EndpointResponsesDoc.tsx](../../../web/src/pages/apiReference/EndpointResponsesDoc.tsx)
  (211 lines) — the read-only "Responses" documentation list (left column).
  Pure presentation; props = `responseEntries: ResponseEntry[]`.
- New [web/src/pages/apiReference/EndpointTryItPanel.tsx](../../../web/src/pages/apiReference/EndpointTryItPanel.tsx)
  (478 lines) — the interactive "Try it" panel (right column): path/query
  parameter inputs, request-body editor, send/curl actions, and the
  response/recovery surface. All state stays in the parent card; values +
  setters are threaded via explicit props so the render is unchanged.
- [web/src/pages/apiReference/EndpointCard.tsx](../../../web/src/pages/apiReference/EndpointCard.tsx)
  (977 → 388 lines) keeps the card shell, header, parameters table, and all the
  state/memos, and composes the two new sub-components. The public
  `<EndpointCard>` props (`ep`, `baseUrl`, `proxyInfo`, `dashboardApi`, `id`)
  are unchanged, so its three consumers (`CoreApiSection`, `TagSection`,
  `ApiReferenceSidebar`) are untouched. Also removed a dangling doc comment that
  documented a `safeParseJson` helper already moved to `endpointResponseHelpers`.

## Validation evidence

- `cd web && npm run build` — clean (tsc typecheck + vite bundle); all prop
  wiring type-checks.
- `cd web && npx eslint <the 3 files>` — clean.
- `cd web && npx vitest run` — **823 passed** (93 files); the `apiReference`
  suite (39 tests / 8 files) renders `EndpointCard` and stayed green → no
  consumer regression.
- Visual smoke: the `/docs` API Reference page renders cleanly with no crash or
  error boundary on the local host-mode dev server (the EndpointCard module
  graph imports fine via `CoreApiSection`/`TagSection`). A populated, expanded
  card requires a configured cluster context, which is unavailable in the
  local network-blocked session; the verbatim relocation + build + vitest
  evidence covers the render parity.

## Remaining #24 work (still deferred)

- Component splits `ProvisionModal.tsx`, `ClusterBento.tsx` — separate scoped PRs.
- `prepare_db.py` `_try_dispatch_aks_mode` full body extraction (HTTPException-
  coupled; needs a domain-error/result-object boundary).
