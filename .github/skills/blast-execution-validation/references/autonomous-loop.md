# Autonomous Run, Fix, and Rerun Loop

## Loop Contract

The agent should keep working until the selected scope is green, cost approval is required, login expires, or an external service blocks progress.

Use this loop for each scenario:

1. Announce the next scenario and why it is the next lowest-risk step.
2. Run the scenario with a generous timeout appropriate to its tier.
3. If it passes, record evidence and advance.
4. If it fails, stop the scenario sequence and classify the failure.
5. Collect targeted logs and telemetry.
6. Fix the root cause in the smallest responsible module.
7. Add or update a test that would have caught the failure.
8. Rerun the targeted test, then rerun the failed scenario.
9. Continue only after the scenario is green or explicitly blocked.

## Failure Classification

- `auth`: 401, wrong tenant, expired token, missing local MSAL setup, missing `E2E_BEARER_TOKEN`.
- `validation`: 422, schema mismatch, UI payload drift from API model, pre-flight/submit parity failure.
- `queue`: broker unavailable, task not routed to `blast`, stale queued rows, missing task id, worker prefetch/concurrency surprise.
- `runtime`: terminal exec refusal, `elastic-blast` CLI error, Kubernetes scheduling, DB warmup, result finalization.
- `openapi`: sibling URL/token missing, upstream 4xx/5xx, status vocabulary drift, OpenAPI rate limit.
- `storage`: private endpoint/network blocked, RBAC, missing database files, sharding metadata mismatch.
- `ui`: React exception, typed client mismatch, stale query cache, unhandled API error body, Playwright flake.
- `telemetry`: missing App Insights connection string, missing server role, exceptions/traces after an otherwise green scenario.

## Log Collection

Local host-mode logs are under `.logs/local/latest/`. Check only the relevant tails:

```bash
tail -n 160 .logs/local/latest/api.log
tail -n 160 .logs/local/latest/worker.log
tail -n 160 .logs/local/latest/beat.log
tail -n 160 .logs/local/latest/web.log
tail -n 160 .logs/local/latest/terminal-exec.log
```

For deployed Container Apps, use targeted sidecar logs and redact sensitive values:

```bash
az containerapp logs show \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --container api \
  --tail 200

az containerapp logs show \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --container worker \
  --tail 200
```

## Critic Pass Before Success

Ask these questions before declaring the run green:

- Did dashboard submit and OpenAPI submit both exercise their intended paths?
- Did queue depth and queue position make sense under parallel submit load?
- Did idempotency prevent uncontrolled duplicates?
- Did invalid payloads return structured 4xx responses instead of 500s?
- Did rate limiting return 429 only when expected?
- Did `/api/blast/jobs` merge local and external jobs without duplicates?
- Did UI toasts and pages show useful errors instead of raw `HTTP 422` or blank states?
- Did App Insights show no new server-side exceptions, failed requests, failed dependencies, or high-severity traces during the test window?
- Were logs sanitized of tokens, SAS signatures, subscription ids, and UPNs before reporting?
- Did every behavior-changing fix include a targeted test and feature change note?

## Escalation Points

Stop and ask the user before:

- Running `full-azure` lifecycle when the slash invocation did not explicitly set `scope: full-azure`. If the invocation did set `scope: full-azure`, treat that as approval for the guarded validation scenario only.
- Running any command that can create material Azure cost outside the selected scope, requested concurrency, or requested time budget.
- Redeploying Container Apps, building ACR images, running `azd provision`, or changing sidecar layout.
- Deleting live jobs, databases, clusters, storage accounts, or resource groups.
- Requesting or handling secrets.

Continue without asking when:

- Running local-safe tests, lint, unit tests, mocked Playwright scenarios, or read-only Azure/App Insights queries after login is established.
- Editing code to fix an observed failure within the requested validation scope.
- Rerunning the failed test/scenario after a fix.

## Budget Enforcement

For an invocation such as `/blast-execution-validation scope: full-azure concurrency=2 max-hours=4`, create a four-hour deadline before the first command. Before each live scenario, estimate whether the remaining time can cover the scenario and its cleanup/telemetry pass. If not, stop and report the skipped scenario as `blocked_by_budget`.

The lifecycle smoke is serialized even when `concurrency` is greater than one. Use the requested concurrency only for follow-up submit fan-in probes, and never start additional parallel submits if fewer than 30 minutes remain.