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

Added a separate guarded `azure-core-nt-lifecycle` scenario for the costly path:
provision/start AKS, prepare `core_nt`, build shard layouts, warm the database,
then submit and wait for a small BLAST job. It requires
`E2E_ALLOW_AZURE_LIFECYCLE=1` and
`E2E_CONFIRM_AZURE_COSTS=create-core-nt-shard-warmup-blast`.

## API/IaC diff summary

No API or infrastructure resources changed. The script only wraps existing
local development helpers and updates local `.env` files for bypass mode.
`@playwright/test` was added as a web dev dependency, with Playwright output
directories ignored in git. `scripts/dev/e2e-ui.sh --fullstack` now starts
redis, api, worker, beat, web, and terminal-exec for scenarios that enqueue
Celery work. The AKS SKU catalog now includes D/E as v7 entries so lifecycle
tests can run in subscriptions where D/E v3/v5 are restricted.

The lifecycle dry run also hardened existing backend/dev-loop behavior:
`provision_aks` now preserves an existing resource group's immutable location
instead of re-submitting it with the target AKS region, the E2E launcher maps
`E2E_STORAGE_ACCOUNT` into `ELB_LOCAL_STORAGE_ACCOUNT` so local worker state
uses the intended Storage account, and `prepare-db` copy-status polling can be
batched via `PREPARE_DB_COPY_POLL_BATCH_SIZE`. The lifecycle scenario stops AKS
during the Storage-only prepare/shard phase by default and restarts it for
warmup/BLAST. A live `core_nt` run also found that prepared `core_nt` Storage
does not necessarily include `taxdb.*`; warmup now treats taxdb files as
optional cache extras instead of failing node-local warmup when the DB prefix is
otherwise complete. The terminal ElasticBLAST patcher also adds the dashboard's
D/E `as_v7` Azure VM SKUs to ElasticBLAST's Azure machine catalog so submit uses
the same SKU set that AKS provisioning and warmup accept.
Cleanup also hardened `storage-public-access.sh off` to update and verify
`publicNetworkAccess=Disabled` and `defaultAction=Deny` together after a local
debug session. Follow-up restart testing also split OpenAPI job-list calls onto
a short timeout so `/api/blast/jobs` does not hang for 90 seconds when the
optional OpenAPI list endpoint is unreachable, and submit recovery now trusts
Kubernetes state if `elastic-blast submit` returns non-zero after creating a
running/completed BLAST workload.

## Validation evidence

- Passed: `bash -n scripts/dev/e2e-ui.sh`.
- Passed: `scripts/dev/e2e-ui.sh --help`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- true`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- sh -c 'printf ...'` confirmed scenario commands receive `E2E_BASE_URL`, `E2E_API_URL`, `E2E_AUTH_MODE`, `E2E_BROWSER_MODE`, `HEADLESS`, and `PLAYWRIGHT_HEADLESS`.
- Passed: `npm --prefix web run e2e:list` loaded 5 tests across 4 scenario files.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:dashboard`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:new-search`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:api-blast` (pre-flight passed; real submit skipped until `E2E_ALLOW_BLAST_SUBMIT=1`).
- Passed: `npm --prefix web run e2e:azure-core-nt-lifecycle` without lifecycle flags skipped the costly Azure scenario.
- Passed: live opt-in `azure-core-nt-lifecycle` provisioned/reused `aks-elb-e2e-core-nt` in `eastus2` with `Standard_D2as_v7` system pool and `Standard_E32as_v7` blast pool, completed `core_nt` Storage prepare, built shard layouts, warmed all 3 nodes, and completed the sharded `core_nt` BLAST smoke (`1 passed`, 1.3 min on the final cached rerun).
- Passed: restart follow-up `azure-core-nt-lifecycle` after AKS stop/start. Stale warmup was released, all three `core_nt` warmup jobs completed on the new nodes, and the sharded BLAST smoke completed (`1 passed`, 1.8 min after warmup cache was rebuilt).
- Passed: guarded API smoke with `E2E_ALLOW_BLAST_SUBMIT=1`; pre-flight and real `/api/blast/jobs` submit both passed, and the created Kubernetes BLAST/finalizer jobs completed.
- Passed: `uv run pytest -q api/tests/test_azure_provision_aks.py api/tests/test_aks_skus.py`.
- Passed: `uv run pytest -q api/tests/test_warmup_jobs.py api/tests/test_terminal_patch_elastic_blast.py`.
- Passed: final targeted validation `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py api/tests/test_aks_skus.py api/tests/test_azure_provision_aks.py` (45 passed).
- Passed: final expanded targeted validation `uv run pytest -q api/tests/test_external_blast_api.py api/tests/test_terminal_patch_elastic_blast.py api/tests/test_warmup_jobs.py api/tests/test_aks_skus.py api/tests/test_azure_provision_aks.py` (87 passed).
- Passed: targeted `ruff check` for the changed AKS/provision/prepare files.
- Passed: targeted `ruff check` for warmup script and terminal patch taxdb handling.
- Passed: `/api/blast/jobs?...&limit=10` returned in 0.388 s after OpenAPI list timeout hardening, with `external_degraded_reason=openapi_unreachable` instead of blocking the Jobs surface.
- Cleanup: `az aks stop -g rg-elb-dashboard -n aks-elb-e2e-core-nt --no-wait` issued after validation; `az aks show` reported `powerState=Stopped`.
- Cleanup: `scripts/dev/storage-public-access.sh off --account stelbdashboardmul5oh5j44 --rg rg-elb-dashboard --subscription 577d6332-de48-4a30-be66-dded26a712ea` left Storage at `public=Disabled`, `defaultAction=Deny`, `ipRules=[]`.
- Passed: `scripts/dev/e2e-ui.sh bypass --headless --skip-restart -- npm --prefix web run e2e:all` after adding `azure-core-nt-lifecycle` (3 passed, 2 guarded scenarios skipped).
- Passed: `npm --prefix web run build`.