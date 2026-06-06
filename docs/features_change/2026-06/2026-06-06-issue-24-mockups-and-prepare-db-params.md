# #24 — Retire mockup pages + extract prepare-db AKS Job params

## Motivation

Issue #24 tracks splitting oversized / mixed-responsibility files. Priority 1
(`SettingsPanel.tsx`) had already shipped in `fdd707b`. This change addresses two
of the remaining items:

1. The `web/src/pages/mockups/` design-exploration pages (deletion candidates).
2. A focused slice of the Priority 2 `prepare_db.py` route → service extraction.

## User-facing change

None. This is a pure refactor / dead-code removal — no behaviour change, no API
or UI change. The retired `/mockups/*` routes were design-exploration throwaways
never linked from the app navigation.

## What changed

### Mockups retired (~5,400 lines of dead code removed)

- Deleted the four route-only prototype pages: `AksCardMockups`,
  `AksCardMockupsRefined`, `AksCardMockupsPremium`, `AksCardMockupsSimple`
  (~5,041 lines) and their `/mockups/aks-card*` routes + imports in
  [web/src/App.tsx](../../../web/src/App.tsx).
- `SidecarInspectorMockups.tsx` could **not** simply be deleted: its `VariantA`
  component and `InspectorRequest` type are used in production by
  [HttpInspectorPanel.tsx](../../../web/src/components/cards/SidecarsCard/HttpInspectorPanel.tsx).
  Moved the file to
  [web/src/components/cards/SidecarsCard/sidecarRequestInspector.tsx](../../../web/src/components/cards/SidecarsCard/sidecarRequestInspector.tsx),
  kept Variant A + its helpers verbatim, and removed the two unshipped variants
  (B/C), the demo fixture generator, and the demo page wrapper. Updated the two
  comments that referenced the old mockup paths.

### prepare_db.py — env-driven Job params moved to a service

- The AKS-fanout `_try_dispatch_aks_mode` is a ~490-line function that mixes HTTP
  validation, RBAC pre-flight, NCBI listing, metadata I/O, locking, and Celery
  dispatch — most of it tightly coupled to `HTTPException`, so a wholesale
  extraction would be high-risk for zero behaviour gain.
- Extracted the **safe, side-effect-free slice**: the `PREPARE_DB_AKS_*`
  environment-knob parsing (~60 lines of `int()`/clamp/`None`-default logic) into
  [api/services/storage/prepare_db_aks_params.py](../../../api/services/storage/prepare_db_aks_params.py)
  `resolve_aks_job_limits() -> AksJobLimits`. The route now calls it and spreads
  `limits.task_overrides()` into the Celery kwargs, preserving the exact
  "unset/unparsable override → builder default" contract (including the
  `backoff_limit=0` edge case).

## API / IaC diff summary

- No route, response, or env-var contract change. New pure service module +
  unit test. No new dependency.

## Validation evidence

- Backend: `uv run ruff check api` clean; `uv run pytest -q api/tests` →
  **3021 passed, 3 skipped**. New `test_prepare_db_aks_params.py` (4 cases);
  `test_prepare_db_hardening.py` + `test_storage_data.py` green (no regression).
- Frontend: `cd web && npm run build` clean; `npx vitest run` → **706 passed**;
  no remaining `pages/mockups` import references.

## Remaining #24 work (deliberately deferred, see issue comment)

- `prepare_db.py` `_try_dispatch_aks_mode` full body extraction — high-risk,
  `HTTPException`-coupled; warrants its own focused PR with a domain-error /
  result-object boundary.
- Priority 2 frontend splits (`EndpointCard`, `ProvisionModal`, `ClusterBento`,
  `blast.ts` type extraction) — pure structural refactors; risk/value favours
  separate scoped PRs over batching into this change.
