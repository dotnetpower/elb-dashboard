# 2026-05-25 — `quick-deploy.sh` env-leak hardening + e2e `recent-failed-provisions` mock

## Motivation

The 2026-05-25 BLAST execution validation run (see [2026-05-25-blast-execution-validation-full-azure.md](2026-05-25-blast-execution-validation-full-azure.md)) surfaced two real bugs that we agreed to fix at the root:

1. **P0-B — `VITE_AUTH_DEV_BYPASS=true` leaked into the cloud frontend.** Revision `0000006` of `ca-elb-dashboard-01` was deployed with `VITE_AUTH_DEV_BYPASS=true`. The SPA therefore skips MSAL while the `api` sidecar still enforces bearer tokens, so any real user hits a sea of 401s. The leak came from `scripts/dev/quick-deploy.sh::load_simple_env_file()` using the `[[ -z "${!key:-}" ]]` guard, which treats unset and `VAR=""` identically — exactly the same root cause as the 2026-05-21 `VITE_API_BASE_URL` regression.
2. **P1 — Three e2e:safe scenarios trip `assertNoErrorBoundary`.** Three `monitor-jobs-events.ui` / `layout-navigation.ui` failures were actually caused by the rendered `ClusterCard` hydrating from the real local `/api/aks/recent-failed-provisions` endpoint (a stale 2026-05-24 failed row), which rendered a `getByRole('alert')` "Provisioning failed." banner. No Playwright mock existed for this endpoint.

## User-facing change

* **Cloud frontend deploys can no longer ship `VITE_AUTH_DEV_BYPASS=true` without an explicit override.** `scripts/dev/quick-deploy.sh frontend` aborts with a clear error before any `az acr build` runs. Escape hatch: `ELB_ALLOW_AUTH_BYPASS_IN_CLOUD=1` (intentionally undocumented in `--help` so it never becomes a normal workflow).
* **`web/.env.local` local-debug values stop leaking into cloud deploys.** `VITE_AUTH_DEV_BYPASS` and `AUTH_DEV_BYPASS` join `VITE_API_BASE_URL` on the skip-list for that file.
* **`load_simple_env_file()` now respects empty-string exports.** A caller's deliberate `export VAR=""` is preserved instead of being silently overwritten by a `.env` file value. This fix also retroactively prevents future regressions of the same class (the 2026-05-21 `VITE_API_BASE_URL` bug + this 2026-05-25 `VITE_AUTH_DEV_BYPASS` bug share this root cause).
* **e2e `recent-failed-provisions` is mocked.** `scripts/e2e/fixtures/mockApi.ts` and `scripts/e2e/scenarios/helpers/apiMocks.ts` both stub the endpoint with `{ jobs: [], degraded: false }`. The 3 e2e:safe failures caused by stale local jobstate are eliminated.

## API / IaC / config diff summary

* `scripts/dev/quick-deploy.sh` — three edits:
  * `load_simple_env_file()` guard: `[[ -z "${!key:-}" ]]` → `[[ -z "${!key+x}" ]]`.
  * Skip-list for `web/.env.local` extended: `VITE_API_BASE_URL VITE_AUTH_DEV_BYPASS AUTH_DEV_BYPASS`.
  * New `frontend`-sidecar cloud-deploy guard: dies if `VITE_AUTH_DEV_BYPASS_VAL == "true"` unless `ELB_ALLOW_AUTH_BYPASS_IN_CLOUD=1`.
* `scripts/e2e/fixtures/mockApi.ts` — added `await page.route("**/api/aks/recent-failed-provisions**", (route) => jsonResponse(route, { jobs: [], degraded: false }));` after the `service-ip` stub (line ~327).
* `scripts/e2e/scenarios/helpers/apiMocks.ts` — same mock added inside `installNewSearchApiMocks` after the `warmup-status` stub.
* No Bicep, no Python, no SPA source changed. No new dependency. No image rebuild required to land the script/test changes; image-rebuild for the env-leak fix is a separate decision (the broken `0000006` revision is still live in `ca-elb-dashboard-01`).

## Validation evidence

* `bash -n scripts/dev/quick-deploy.sh` → `bash-syntax-ok`.
* `grep -n 'VITE_AUTH_DEV_BYPASS\|_key+x\|AUTH_DEV_BYPASS' scripts/dev/quick-deploy.sh` confirms the new skip-list (lines 150-151), guard (line 244-245), and the original build-arg / set-env passthrough (lines 254, 316) all reference the same `$VITE_AUTH_DEV_BYPASS_VAL`.
* `cd web && npm run e2e:all-safe` (project set `ui-mock + api-smoke + mutation-mock`):
  * Before: 6 failed / 9 passed / 1 skipped.
  * After: **3 failed / 12 passed / 1 skipped** (`recent-failed-provisions`-caused failures are gone).
  * Remaining 3 failures (`monitor-jobs-events.ui.spec.ts:3:5` Live Wall heading, `:16:5` `Today` button, `layout-navigation.ui.spec.ts:12:5` top-nav) are **unrelated** to this fix — they are caused by Live Wall being a preview-flag route and other UI state, and will be tracked as a separate P2 e2e gap.
* Local `curl -fsS 'http://127.0.0.1:8085/api/aks/recent-failed-provisions?hours=24&limit=10'` still returns the stale row (expected: this fix is about mocking the endpoint in e2e, not purging the local jobstate row).
* Storage posture verified closed: `{'public': 'Disabled', 'default': 'Deny', 'bypass': 'AzureServices', 'ipRules': 0}` for `stelbdashboard01mul5oh5j`.

## What is NOT in this change

* **No frontend redeploy.** `ca-elb-dashboard-01` revision `0000006` still carries the leaked `VITE_AUTH_DEV_BYPASS=true`. Redeploying to flush the leak is a separate, explicit decision per the charter's "Do NOT redeploy for ordinary code changes" rule.
* **No purge of the stale jobstate row.** The `recent-failed-provisions` fix is at the e2e layer, where mocking is the correct contract. The local row will age out naturally; if the user wants it gone they can hit the existing dismiss UI.
* **No fix for the remaining 3 e2e:safe failures.** They are a distinct preview-flag / UI gap and will be addressed under a separate change note.

## Risk

Low. The shell guard change (`${!key+x}`) is strictly more correct than the prior form and only affects callers that explicitly export an empty value — exactly the case `quick-deploy.sh` must respect. The frontend cloud-deploy guard fails closed with a clear error and an explicit override. The Playwright mock addition is additive and only changes behaviour when the endpoint is hit during a mocked run.
