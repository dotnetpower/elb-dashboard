# BLAST Results Hardening

**Date**: 2026-05-13

## Motivation

The BLAST job detail page could hide useful failure context when a Durable orchestration ended with a generic `error` phase. Report export links also opened protected API routes without bearer-token headers, which made CSV/JSON exports fail for signed-in users.

## User-facing Change

- Failed BLAST jobs now infer the failed execution step from stored step data, output, and orchestration metadata.
- The failed step is expanded with the best available error text instead of rendering the whole timeline as skipped.
- Terminal failed jobs no longer show the Cancel action or running-only results copy.
- Results empty states name the inferred failed step.
- CSV and JSON exports now download through the authenticated API client and show a loading state.

## API / IaC Diff

- `web/src/api/client.ts` — added authenticated text-response support for non-JSON API responses.
- `web/src/api/endpoints.ts` — added typed BLAST export API helper and corrected the legacy export URL path.
- `web/src/pages/BlastResults.tsx` — hardened failed-step inference and replaced unauthenticated export anchors with authenticated download buttons.
- No IaC changes.

## Validation

- `npx prettier --write src/api/client.ts src/api/endpoints.ts src/pages/BlastResults.tsx`
- `npm run build`: passed.
- `azd deploy web --no-prompt`: deployed to `https://kind-coast-0eb698500.7.azurestaticapps.net/`.
- Browser verification on `/blast/jobs/job-8e7f852e3406`: the page shows `Job Failed at Warmup`, hides the Cancel action, expands the Warmup failure log, marks later steps skipped, and shows `No results available — the job failed at the Warmup step.`
- `npm run lint`: not runnable because the project has ESLint 9 but no `eslint.config.*` file.