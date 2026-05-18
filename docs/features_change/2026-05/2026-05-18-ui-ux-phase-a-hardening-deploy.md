# 2026-05-18 — UI/UX Phase A: critical hardening pass + deploy + Jobs empty-state diagnosis

## Motivation

Following the Phase A UI/UX improvements (see
[2026-05-18-ui-ux-phase-a.md](./2026-05-18-ui-ux-phase-a.md)), the user asked
for three follow-ups in one pass:

1. Critical hardening — explicit security / a11y / cross-session-conflict
   audit on every file that Phase A added or changed.
2. Deploy the changes to the `elb-ca` Container App.
3. Investigate why the Jobs page appears empty and remediate.

## User-facing change

* Same behaviour as Phase A. No new features.
* The Jobs page now renders correctly in both states — when the backend has
  no `AZURE_TABLE_ENDPOINT` (local compose without a Table Storage endpoint)
  it shows the new `DegradedNotice` with the operator-actionable message
  `Job state storage is not configured. Set AZURE_TABLE_ENDPOINT…`; when the
  backend is configured but has zero submitted jobs, it shows
  `No BLAST jobs yet.` plus the Submit-your-first-search CTA.
* The deployed Container App `ca-elb-control` on `elb-ca` carries the Phase A
  bundle (DegradedNotice everywhere, URL-synced Jobs filters, az login
  freshness probe on the Terminal cockpit, API Reference sidebar, sanitised
  error toasts, Custom DB wizard stepper, draft-saved + pre-flight gates).

## API / IaC diff summary

No API or IaC changes in this follow-up. The Bicep already wires
`AZURE_TABLE_ENDPOINT` for both `api` and `worker` sidecars (see
[infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep)
lines 161 and 248), so production is unaffected by the local-only
`not_configured` degraded state observed during diagnosis.

## Critical hardening audit

Files in scope (Phase A):

* `web/src/components/DegradedNotice.tsx`
* `web/src/components/RowSkeleton.tsx`
* `web/src/pages/apiReference/ApiReferenceSidebar.tsx`
* `web/src/pages/BlastJobs/useBlastJobsState.ts`
* `web/src/pages/BlastJobs/BlastJobs.tsx`
* `web/src/pages/BlastJobs/JobsEmptyState.tsx`
* `web/src/pages/terminal/TerminalCockpit.tsx`
* `web/src/api/client.ts`
* `api/routes/terminal_ws.py`

Audit results:

| Check                                                                    | Result |
| ------------------------------------------------------------------------ | ------ |
| No `azure.functions` import                                              | OK — none of the Python files import `azure.functions`. |
| No bare `from services.X` / `from auth.X` import                         | OK — `api/routes/terminal_ws.py` uses `from api.auth import …` and `from api.services.terminal_exec import …`. |
| No SAS issuance to the browser                                           | OK — none of the changed files import `generate_blob_sas`, `get_user_delegation_key`, or `BlobSasPermissions`. The new `/api/terminal/azure-cli` route returns only `subscription_id` / `tenant_id` / `user.name` / `user.type`, which are already exposed via `/api/me` and `/api/arm/subscriptions`. |
| ttyd loopback unchanged                                                  | OK — no changes to `terminal/entrypoint.sh` or the ttyd bind address. |
| Storage `publicNetworkAccess` left at `Disabled`                          | OK — no changes to `infra/modules/storage.bicep`, `api/services/storage_data.py`, or `api/services/storage_network.py`. |
| MSAL bearer enforced on new HTTP route                                   | OK — `/api/terminal/azure-cli` declares `caller: CallerIdentity = REQUIRE_CALLER`, matching every other authenticated route. |
| `terminal_exec` allowlist respected                                      | OK — the new route only calls `terminal_exec.run(["az", "account", "show", "-o", "json"])`. `az` is in the `argv[0]` allowlist defined in `api/services/terminal_exec.py`. |
| Output sanitisation on UI error toasts                                   | OK — `sanitiseUserFacingMessage` in `web/src/api/client.ts` redacts SAS query strings (`sig=…`), bearer tokens, GUIDs, and humanises ARM `(ResourceNotFound)` / `(AuthorizationFailed)` prefixes before any toast is rendered. |
| a11y — degraded state announced                                          | OK — `DegradedNotice` renders inside `role="status"` with `aria-live="polite"`. Status icon has `aria-hidden`. |
| a11y — loading state announced                                           | OK — `RowSkeleton` wraps the placeholder rows in `role="status"` + `aria-live="polite"` + the visually-hidden `label` text. |
| a11y — chip / stepper controls                                           | OK — Custom DB wizard stepper uses `aria-current="step"` on the active step; Jobs status chips set `aria-pressed`; nav uses `aria-label`. |
| a11y — sidebar search                                                    | OK — input has an associated `<label>` (wrapping pattern) and a clear-button with `aria-label`. |
| Cross-session conflict — concurrent session files                        | OK — `git status -s` shows no overlap with `api/routes/stubs.py`, `api/services/storage_data.py`, `api/services/blast_db_metadata.py`, `api/services/blast_oracles.py`, `api/tasks/blast.py`, or `api/tests/test_blast_*.py`. |
| Order in `api/main.py`                                                   | OK — `/api/terminal/azure-cli` is registered via the existing `terminal_ws.router`, which is mounted **above** the `frontend_proxy.router` catch-all. |

Repo-policy items not in scope for these files but re-verified:

* English-only in every file (no Korean string, identifier, or comment).
* No `requirements.txt` written, no `pip install`, no `func start`.
* No new dependency added to `pyproject.toml` or `web/package.json`.

## Jobs empty-state diagnosis + remediation

Symptom (local Docker Compose, `compose-full`):

```bash
curl -s http://127.0.0.1:18080/api/blast/jobs | jq
{
  "jobs": [],
  "degraded": true,
  "degraded_reason": "not_configured",
  "message": "Job state storage is not configured. Set AZURE_TABLE_ENDPOINT to connect to Azure Table Storage."
}
```

Root cause:

* `api/services/state_repo.py` reads `_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"`
  (line 24). When absent it returns `degraded_reason="not_configured"`.
* `scripts/dev/docker-compose.full.yml` does **not** set `AZURE_TABLE_ENDPOINT`
  for the `api` / `worker` sidecars (the local stack has no Table Storage
  endpoint — the bundled Azurite container is on the host's `bridge`
  network, not on the compose `elb` network).
* The deployed Container App is unaffected because
  `infra/modules/containerAppControl.bicep` already exports
  `AZURE_TABLE_ENDPOINT` for both the `api` and `worker` sidecars (lines 161
  and 248).

UI behaviour validation:

* Navigated to `http://127.0.0.1:18080/blast/jobs` in the running compose
  stack via Playwright. The page now renders:
  * Header counts `0 total · 0 running · 0 completed · 0 failed`.
  * `No BLAST jobs yet.` paragraph.
  * The new `DegradedNotice` with severity `degraded`, title
    `Job listing degraded`, message
    `Job state storage is not configured. Set AZURE_TABLE_ENDPOINT to connect to Azure Table Storage.`
  * `Submit your first search` CTA.

Remediation:

* **Production path** (the user's stated requirement): no code change needed.
  The deployed Container App reads `AZURE_TABLE_ENDPOINT` from the Bicep
  output, so the Jobs page will show real submissions instead of the
  degraded state as soon as `azd up` finishes redeploying the new SPA bundle
  + API image.
* **Local-compose path** (out of scope for this change): noted in
  `AGENTS.md` §"Validation cheatsheet" — a future change can either add an
  `azurite` service to `compose.full.yml` on the `elb` network and extend
  `state_repo.py` to accept `AZURE_TABLE_CONNECTION_STRING`, or document
  the `not_configured` state as expected for local development.

## Validation evidence

Backend / build:

```bash
uv run pytest -q api/tests        # 635 passed in 37.65s (Phase A baseline)
uv run ruff check api             # All checks passed!
cd web && npm run build           # built in 8.89s (Phase A baseline)
```

Live browser check (compose, before deploy):

* `GET /api/blast/jobs` → `degraded_reason="not_configured"` as expected.
* `/blast/jobs` page renders header, counts, `No BLAST jobs yet.`,
  `DegradedNotice`, and the Submit CTA — confirming the new empty-state UI.

Deploy:

* `azd env select elb-ca && azd up --no-prompt`
  * Subscription: `ME-MngEnvMCAP132261-moonchoi-1`
  * Resource group: `rg-elb-ca`
  * Container App: `ca-elb-control`
  * FQDN:
    `ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io`
* Bicep diff: empty (no IaC changes in Phase A).
* Postprovision rebuilt and pushed three images (`elb-api`, `elb-frontend`,
  `elb-terminal`) via `az acr build` in parallel (image tag
  `20260518124305`), then applied the six-sidecar yaml via a one-shot Bicep
  deployment (`ca-swap-20260518124305`).
* Total `azd up` wall-time: **9 minutes 22 seconds**. Image build phase
  6m00s (elb-frontend first at 1m51s, elb-api at 2m53s, elb-terminal at
  6m00s), template swap 51s, `/api/health` healthy on attempt 1.
* New active revision: `ca-elb-control--0000060` (createdTime
  `2026-05-18T12:49:31Z`, replicas = 1).

Post-deploy verification:

* `GET https://<fqdn>/api/health` → `200 OK` (postprovision health probe,
  re-asserted manually afterwards).
* `GET https://<fqdn>/api/blast/jobs` anon → `401 missing bearer token`
  (MSAL gating intact, no information leak).
* `az containerapp show -n ca-elb-control -g rg-elb-ca --query
  "properties.template.containers[?name=='api'].env[]"` confirms
  `AZURE_TABLE_ENDPOINT = https://stelbnm5virmqrdi5c.table.core.windows.net`
  and `AZURE_BLOB_ENDPOINT = https://stelbnm5virmqrdi5c.blob.core.windows.net`
  on the new revision. Both env vars are also present on the `worker`
  container (verified the same way).
* ACR public-network-access restored to `Disabled` by the
  `restore_acr_network` EXIT trap.

Production Jobs page expected behaviour:

* Backend `state_repo.list_jobs()` now reaches Azure Table Storage via the
  shared user-assigned MI (`id-elb-control`) and the private endpoint
  `pe-stelbnm5virmqrdi5c-table`. No SAS issuance, no public-network access.
* If no BLAST jobs have been submitted yet from this revision, the UI shows
  the new `NoJobsEmpty` panel with `No BLAST jobs yet.` + the
  `Submit your first search` CTA — and **no** `DegradedNotice`, because
  `degraded === false` in the backend payload.
* If the Table Storage call fails at runtime (transient network /
  authentication issue) the UI shows `NoJobsEmpty` plus the
  `DegradedNotice` with the operator-actionable message returned by the
  backend (e.g. `Could not reach Azure Table Storage…`).
