# UI E2E launcher

## Motivation

Local UI checks needed one entry point that can start the dashboard either in
dev-bypass mode for agent-driven testing or in real MSAL login mode for
user-assisted authentication checks.

## User-facing change

Added `scripts/dev/e2e-ui.sh` with `bypass`, `login`, `off`, and `status`
actions. The launcher chooses headed or headless browser mode from explicit
flags, a short interactive prompt, or CI/non-interactive defaults. Scenario
commands passed after `--` receive `E2E_BASE_URL`, `E2E_API_URL`,
`E2E_AUTH_MODE`, `E2E_BROWSER_MODE`, `HEADLESS`, and Playwright-compatible
headless environment variables.

Added [Playwright](https://playwright.dev/) E2E scenarios for dashboard route
smoke, BLAST API pre-flight / guarded submit, and New Search option payload
matrix checks. The real submit case is guarded by `E2E_ALLOW_BLAST_SUBMIT=1` to
avoid accidental [Azure](https://azure.microsoft.com/) costs; login-mode API
requests can pass `E2E_BEARER_TOKEN` because the smoke uses Playwright's API
request context rather than the SPA MSAL cache.

## API/IaC diff summary

No API or infrastructure resources changed. The script only wraps existing
local development helpers and updates local `.env` files for bypass mode.
`@playwright/test` was added as a web dev dependency, with Playwright output
directories ignored in git.

## Validation evidence

- Passed: `bash -n scripts/dev/e2e-ui.sh`.
- Passed: `scripts/dev/e2e-ui.sh --help`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- true`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- sh -c 'printf ...'` confirmed scenario commands receive `E2E_BASE_URL`, `E2E_API_URL`, `E2E_AUTH_MODE`, `E2E_BROWSER_MODE`, `HEADLESS`, and `PLAYWRIGHT_HEADLESS`.
- Passed: `npm --prefix web run e2e:list` loaded 4 tests across 3 scenario files.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:dashboard`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:new-search`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:api-blast` (pre-flight passed; real submit skipped until `E2E_ALLOW_BLAST_SUBMIT=1`).
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:all` (3 passed, 1 guarded submit skipped).
- Passed: `npm --prefix web run build`.