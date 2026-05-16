# 2026-05-16 — Full UI sweep + degraded-state fixes

## Motivation

User asked to test every menu/feature (`모든 메뉴, 모든 기능에 대해 테스트 해보고 필요한 조치 하자`). Did a route-by-route sweep of the SPA (9 routes) against the live `docker-compose` dev stack and applied targeted fixes for the user-facing degradations found.

## Per-route results

| Route | Status | Notes |
|-------|--------|-------|
| `/` Dashboard | OK | All cards render (AKS Running, ACR 4/4 Built, sharded DB chips). Brief "Loading…" on first paint is normal. |
| `/blast/submit` BlastSubmit | OK | 4-step wizard renders. Form interactions not exercised in this sweep. |
| `/blast/jobs` BlastJobs | **FIXED** | (1) `New search` / `Submit your first search` were disabled while `useClusterReadiness()` was loading or errored. Now enabled when status is unknown — backend gates submission anyway. (2) Honest degraded banner already shown (`AZURE_TABLE_ENDPOINT` not set in dev). |
| `/blast/jobs/:jobId` BlastResults | **FIXED** | When `/api/blast/jobs/{id}/status` returned 503/404 the page stayed in "Loading job details…" forever. Now distinguishes `isLoading` vs `isError` and renders a clear "Job not available" panel with the upstream error and a back-link. |
| `/blast/jobs/:jobId/analytics` BlastAnalytics | not exercised | Requires a completed job; covered by `BlastResults` family changes. |
| `/blast/databases/build` DatabaseBuilder | OK (preview-only) | Honest "Preview only — backend pending" banner. No regressions. |
| `/tools` ToolsPage | OK (preview-only) | All sub-tools (Cost Estimator, Preprocessor, Primer Design, Taxonomy, Schedules, DB Versions, Audit Trail) marked preview. |
| `/terminal` RemoteTerminal | OK | xterm connects via `/api/terminal/ws`, banner + prompt render, command guard banner correct. |
| `/docs` API Reference | OK | OpenAPI v3.3.0 discovered from `elb-openapi` pod (`http://20.249.147.217`). 12 endpoints, 3 groups. |

### Console noise (cosmetic, not fixed)

- React Router v7 future-flag warnings (`v7_startTransition`, `v7_relativeSplatPath`) — harmless, will be resolved when we adopt the v7 flags or upgrade.
- `ws://localhost:8081/?token=…` ECONNREFUSED — Vite HMR fallback in the docker-compose dev mode (frontend served via reverse proxy, dev-only).
- `ws://127.0.0.1:18080/?token=…` 403 — Vite HMR token request hitting the api sidecar; api correctly rejects WS upgrades to `/`. Not user-visible.

## User-facing change

1. **BlastJobs**: clicking "New search" no longer surprises the user with a disabled button while the cluster status is still being fetched. If the cluster is *known* to be absent or stopped, the button stays disabled with the same explanatory tooltip.
2. **BlastResults**: when a job ID is unknown, deleted, or the backing `/api/blast/jobs/{id}/status` endpoint is degraded, the user now sees a red "Job not available" panel with the upstream error and a "Back to job list" link instead of an infinite loading spinner.

## API/IaC diff summary

No backend or infra changes. Frontend-only:

- `web/src/pages/BlastJobs.tsx` — relax "New search" / "Submit your first search" gating to allow click-through when cluster readiness is unknown (`isLoading || isError`). Two locations.
- `web/src/pages/BlastResults.tsx` — split the `!job` branch into `isLoading` (spinner) and `isError` (named error panel with back-link).

## Validation

- `uv run pytest -q api/tests` → **237 passed** in 17.92s.
- `cd web && npm run build` → built in 4.51s, no TypeScript errors.
- Browser sweep — every route in the table above visited, accessibility tree captured, screenshots verified.

## Follow-ups (deferred)

- Adopt React Router v7 future flags or upgrade to v7 to silence the warning pair.
- Surface a friendlier message on the Dashboard when the subscription dropdown can't enumerate (auth/MI scope problem) instead of the cards staying in "Loading…" forever — needs `useSubscriptions` hook tweak.
- BlastResults: when error panel appears, suppress the `resultsQuery` 30s polling (currently it keeps trying because the `enabled` condition doesn't gate on `jobQuery.isError`). Cosmetic; no user impact today.
