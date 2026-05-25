---
name: blast-execution-validation
description: 'Use when: validating ElasticBLAST execution from the UI, OpenAPI job submit, BLAST queueing, parallel submit behavior, E2E hardening, App Insights errors, or autonomous run/fix/rerun loops. Trigger phrases: blast execution test plan, OpenAPI BLAST submit, parallel BLAST queue, App Insights error hunt, autonomous BLAST E2E.'
argument-hint: 'scope: local-safe | live-submit | full-azure; optional concurrency=N max-hours=N'
---

# BLAST Execution Validation

## Goal

Drive a long-running validation loop for ElasticBLAST execution through both supported submit surfaces:

- Dashboard/UI submit through `/api/blast/jobs` using the local Celery queue path.
- External OpenAPI submit through `/api/v1/elastic-blast/submit` and the sibling execution plane.

Use this skill to plan, run, critique, harden, fix, and rerun tests until the selected scope is either green or blocked by an explicit external prerequisite.

## First Decision

Classify the requested run before executing anything:

- `local-safe`: no real BLAST job creation. Run unit, mocked UI, pre-flight, contract, rate-limit, and non-destructive E2E checks.
- `live-submit`: create small real BLAST jobs against an already prepared deployment. Requires explicit opt-in variables and valid Azure/login context.
- `full-azure`: provision/start Azure resources, prepare/shard/warm DBs, submit BLAST jobs, and inspect App Insights. Requires cost confirmation.

If the user did not specify a scope, start with `local-safe`, then ask before moving to `live-submit` or `full-azure`.

## Argument Handling

Parse slash arguments at the start of the run:

- `scope`: one of `local-safe`, `live-submit`, or `full-azure`. An explicit `scope: full-azure` is cost approval for the guarded `azure-core-nt-lifecycle` validation run only; still ask before redeploying, deleting resources, expanding the DB scope, or increasing the requested concurrency.
- `concurrency`: positive integer. Default to `1`; cap at `4` unless the user explicitly asks for a higher value. In `full-azure`, keep the lifecycle scenario serialized and use this value only for the follow-up parallel submit probes after the deployment is healthy.
- `max-hours`: positive number. Default to `4` for `full-azure`, `2` for `live-submit`, and `1` for `local-safe`. Treat this as a hard run budget: compute a deadline, pass an equivalent command timeout where possible, and stop before starting a new live scenario that cannot reasonably finish within the remaining time.

For `/blast-execution-validation scope: full-azure concurrency=2 max-hours=4`, use this execution shape: establish login, run local-safe baseline, run one serialized full Azure lifecycle smoke within the four-hour budget, then run at most two-way parallel submit probes if the lifecycle smoke leaves enough time and the prepared DB is already healthy.

## Mandatory Operating Rules

- Speak to the user in Korean; keep all files, commands, UI text, commit messages, and docs in English.
- Run one scenario at a time when it mutates Azure resources. Parallelize only read-only checks or explicitly selected parallel-submit probes.
- Do not redeploy for ordinary code changes. Validate with pytest, local fullstack, Playwright, and smoke checks unless the repo charter's redeploy exception applies.
- Never issue SAS URLs to the browser, never open production Storage public access, and never use Azure Run Command.
- Do not revert unrelated dirty files. Read `git status --short` before edits and work around user changes.
- If a command needs a secret, stop and ask the user to type it directly into the terminal. Do not ask for secrets through chat.
- For any behavior-changing code fix, add a change note under `docs/features_change/YYYY-MM/` before the final report.

## Initial Login Gate

For `live-submit` or `full-azure`, establish auth first so the loop can run unattended for several hours:

1. Check `az account show -o none` and `azd auth token --output json` when azd operations may be needed.
2. If Azure CLI is not signed in, start `az login --use-device-code` and ask the user to complete the browser flow. Do not continue live scenarios until `az account show` succeeds.
3. Load deployment values with `azd env get-values` when present. Prefer those values for subscription, resource group, Container App, ACR, Storage, App Insights, and AKS names.
4. For local real-identity debugging, use `scripts/dev/e2e-ui.sh login --fullstack ...`; for anonymous local safe checks, use `scripts/dev/e2e-ui.sh bypass ...`.
5. If a Playwright API-request scenario runs in login mode, provide `E2E_BEARER_TOKEN` from a valid non-secret token acquisition flow or run the scenario through dev-bypass.

## Baseline Commands

Run the smallest useful checks first:

```bash
uv run pytest -q \
  api/tests/test_external_blast_api.py \
  api/tests/test_blast_submit_route_options.py \
  api/tests/test_blast_queue.py \
  api/tests/test_blast_tasks.py \
  api/tests/test_openapi_rate_limit.py

npm --prefix web run test -- usePrerequisites useLatestBlastJob clusterContext aks usePrefetchApiReference

scripts/dev/e2e-ui.sh bypass --headless --fullstack -- \
  npm --prefix web run e2e:all-safe

scripts/dev/e2e-ui.sh bypass --headless --fullstack -- \
  npm --prefix web run e2e:api-blast
```

When `live-submit` is explicitly selected, add the guarded real submit smoke:

```bash
E2E_ALLOW_BLAST_SUBMIT=1 scripts/dev/e2e-ui.sh bypass --headless --fullstack -- \
  npm --prefix web run e2e:api-blast
```

For costly full lifecycle, use the guarded Azure scenario only after the user confirms cost:

```bash
E2E_ALLOW_AZURE_LIFECYCLE=1 \
E2E_CONFIRM_AZURE_COSTS=create-core-nt-shard-warmup-blast \
E2E_LIFECYCLE_POLL_MS=30000 \
scripts/dev/e2e-ui.sh bypass --fullstack --headless -- \
  npm --prefix web run e2e:azure-core-nt-lifecycle
```

Wrap this command in a shell-level timeout derived from `max-hours` when running it autonomously. With `max-hours=4`, do not begin a fresh 24-hour database prepare. If `core_nt` is not already prepared, sharded, and warmable within the budget, report `blocked_by_budget` with the readiness evidence instead of letting the command run past the requested window.

## Progressive Workflow

1. Run the baseline commands.
2. If a failure appears, capture the failing command, status code, request id, job id, task id, and relevant API/worker/web log tail.
3. Diagnose the failing layer: React/UI, typed API client, FastAPI route, service wrapper, Celery task, terminal exec, sibling OpenAPI, Kubernetes, Storage, or telemetry.
4. Fix the root cause with the smallest code change and add/update the targeted test.
5. Rerun the failing targeted test, then the scenario that exposed it.
6. Only after the scenario is green, move to the next scenario in the matrix.
7. Before reporting success, run a critic pass: auth, idempotency, queue visibility, parallel behavior, rate limits, log sanitization, App Insights, and test coverage.

Load the detailed scenario matrix from [scenario matrix](./references/scenario-matrix.md), the App Insights queries from [App Insights KQL](./references/app-insights-kql.md), and the autonomous run loop from [autonomous loop](./references/autonomous-loop.md).

## Completion Criteria

The run is complete only when all selected scenarios are either passed or explicitly blocked with evidence. The final report must include:

- Scope selected and why.
- Commands/scenarios run and pass/fail status.
- Job ids, task ids, request ids, and App Insights time window where available.
- Fixes made, files changed, and tests added or rerun.
- Remaining risks, especially cost, live-deployment, or external sibling OpenAPI dependencies.