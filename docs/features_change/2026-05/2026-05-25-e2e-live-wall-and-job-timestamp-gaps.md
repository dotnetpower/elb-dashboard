# 2026-05-25 — e2e Live Wall preview flag + Today-group timestamp gaps

## Motivation

Follow-up to [2026-05-25-quick-deploy-env-leak-and-e2e-mock-fix.md](2026-05-25-quick-deploy-env-leak-and-e2e-mock-fix.md), which explicitly deferred "3 remaining e2e:safe failures" as a separate P2 gap. This change root-fixes those three failures and adds vitest contract guards so the underlying class of bug cannot regress silently.

Symptoms (from the e2e:safe run on `e2e:all-safe`):

1. `monitor-jobs-events.ui.spec.ts:3:5` — "Live Wall" heading never renders.
2. `monitor-jobs-events.ui.spec.ts:16:5` — "Today" group button never appears under the job timeline.
3. `layout-navigation.ui.spec.ts:12:5` — top-nav Live Wall link not visible.

Both #1 and #3 share root cause: the Playwright fixture in `scripts/e2e/fixtures/uiTest.ts` enabled the `customDb` and `labTools` preview prefs but forgot `liveWall`. Live Wall is gated by `<OptionalFeatureRoute>`, so without the pref the route is hidden — exactly the documented behaviour, but the fixture silently asymmetric.

#2 is a deterministic-clock side effect: the fixture defines `const now = "2026-05-24T10:00:00Z"` and assigns it to *all* mock job timestamps. By the time the test runs that `now` is hours-to-days in the past, so the seeded `completed` job lands in the "Yesterday" bucket instead of "Today" and the assertion can never find its group button.

## User-facing change

* The e2e:safe `monitor-jobs-events.ui` and `layout-navigation.ui` projects now pass without the Live Wall / Today regressions.
* No production runtime behaviour changes. Only the test fixture + a `usePreferences` export visibility change (no SPA behaviour change) + 4 new vitest contract tests.

## API / IaC / config diff summary

* `scripts/e2e/fixtures/uiTest.ts` — add `previewLiveWallEnabled: true,` to the `elb-prefs` seed so all three preview-flag routes are reachable by the mock-mode tests. Source of truth alignment: `PREVIEW_PREF_KEYS` in `web/src/hooks/usePreferences.tsx` enumerates exactly `customDb / labTools / liveWall`.
* `scripts/e2e/fixtures/mockApi.ts` — introduce `const recentNow = new Date(Date.now() - 5 * 60 * 1000).toISOString();` (rolling timestamp, 5 minutes ago) and use it for the `completedJob.created_at` and `completedJob.updated_at` fields only. The other 17 `now` references remain pinned to the deterministic value so snapshot-sensitive mocks keep their stable expected output. The split lets the "Today / Yesterday" date-bucket UI see a fresh timestamp without breaking any test that depends on the deterministic clock.
* `web/src/hooks/usePreferences.tsx` — `const PREVIEW_PREF_KEYS` → `export const PREVIEW_PREF_KEYS`. Visibility-only change; no runtime difference.
* `web/src/hooks/usePreferences.fixtureContract.test.ts` — new vitest file with 4 tests:
  1. Iterate `Object.values(PREVIEW_PREF_KEYS)` and assert each one appears as `<key>: true` in `scripts/e2e/fixtures/uiTest.ts` — guarantees that adding a 4th preview pref to the enum without updating the fixture will fail this guard.
  2. Same assertion for `previewCustomDbEnabled`, `previewLabToolsEnabled`, `previewLiveWallEnabled` explicitly (defence in depth).
  3. Assert `mockApi.ts` defines `const recentNow = new Date(`.
  4. Assert the `completedJob` block in `mockApi.ts` contains both `created_at: recentNow` and `updated_at: recentNow`.
* No Bicep, no Python, no production SPA component changed. No new runtime dependency.

## Validation evidence

* `npm --prefix web run test -- usePreferences.fixtureContract.test.ts`:
  - `4 passed (4 ms)`.
* `npm --prefix web run test` (full vitest suite): `7 files / 23 passed` (up from 19 before the 4 new tests).
* `scripts/dev/e2e-ui.sh bypass --headless --fullstack -- npm --prefix web run e2e:all-safe`:
  - Before fix (baseline from prior session): `6 failed / 9 passed / 1 skipped`.
  - After [2026-05-25-quick-deploy-env-leak-and-e2e-mock-fix.md](2026-05-25-quick-deploy-env-leak-and-e2e-mock-fix.md): `3 failed / 12 passed / 1 skipped`.
  - After this change: **`0 failed / 15 passed / 1 skipped`** across `ui-mock + api-smoke + mutation-mock`. The 1 skip is the `api-blast` live-submit guarded by `E2E_ALLOW_BLAST_SUBMIT=1`, expected.
* `scripts/dev/e2e-ui.sh bypass --headless --fullstack -- npm --prefix web run e2e:api-blast`: `1 passed / 1 skipped` (preflight passes; live submit correctly skipped under default scope).
* `uv run pytest -q api/tests`: `187 passed in 20.71s` (unchanged, sanity).
* Production posture verified: `stelbdashboard01mul5oh5j` `{public:Disabled, default:Deny, ipRules:0}` after the local-debug toggle was closed.

## Hardening (regression prevention)

The two bug classes here are symmetry-of-flags ("when I add a preview pref I must remember to flip it on in the fixture") and time-coupling ("deterministic test clock hides date-bucket bugs"). Both are easy to repeat and impossible to spot in code review without a guard. The new `usePreferences.fixtureContract.test.ts` enforces both invariants from the source-of-truth side, so adding `previewFooEnabled` to `PREVIEW_PREF_KEYS` *will* fail vitest until the fixture catches up, and accidentally pinning the `completedJob` timestamps back to the deterministic `now` will likewise fail. Cheap, fast, no infrastructure change.

## What is NOT in this change

* No frontend redeploy. The 0006 revision's leaked `VITE_AUTH_DEV_BYPASS=true` remains tracked under the prior change note; this fix does not require an image rebuild.
* No fix for the `core_nt` BLAST DB being empty in storage — that is a deployment data state, not a code defect (see "Out of scope" below).
* No App Insights KQL hunt — there are no real BLAST submits in the recent window to investigate.

## Out of scope — Azure live-submit readiness gap

For the record: while assessing full-azure scope per the `blast-execution-validation` skill, the workload Storage account was briefly opened with the documented local-debug toggle (`scripts/dev/storage-public-access.sh on/off`, then revoked the temporary `Storage Blob Data Reader` role) and `blast-db/core_nt/` was confirmed **empty (0 blobs)**. The full-azure scenario cannot run without a pre-prepared DB, and DB preparation is ~24h which is outside the skill's default `max-hours=4` budget. Reported as `blocked_by_budget` in this session's BLAST validation summary — not a code change. Storage and RBAC were both reverted to the production posture before this note was written.

## Risk

Low. Test-only / hook-visibility change. The new tests assert against actual file contents using `node:fs.readFileSync` against repo-relative paths and so are self-pinning to the source of truth.
