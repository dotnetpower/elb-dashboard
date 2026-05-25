# 2026-05-25 — `blast-execution-validation` full-azure run (concurrency=2, max-hours=4)

## Motivation

User invoked `/blast-execution-validation` with `scope: full-azure concurrency=2 max-hours=4`
against the freshly redeployed Container App (`ca-elb-dashboard-01`, revision
`0000006`, image tag `20260525142909`). The slash invocation is the cost
approval for the guarded `azure-core-nt-lifecycle` scenario only — redeploy,
delete, scope expansion, or concurrency bump still require explicit confirmation.

## Validation budget

- Start: 2026-05-25T05:41:01Z UTC
- Deadline: 2026-05-25T09:41:01Z UTC (4h hard cap)
- Local pre-flight elapsed: ~15 min
- Outcome: lifecycle scenario declared `blocked_by_budget`

## Local-safe baseline (gate 1)

| Suite                     | Result          | Evidence                                     |
| ------------------------- | --------------- | -------------------------------------------- |
| `uv run pytest -q api/tests` | 187 / 187 pass | 5 files, fast loop with `-n auto`            |
| `npm --prefix web run test -- --run` | 19 / 19 pass | 7 files                                      |
| `npm --prefix web run test:e2e:safe` | **6 fail / 9 pass / 1 skipped** | mock coverage gap (below)                    |
| `npm --prefix web run test:e2e:api-blast` | 1 skipped, no fail | paid scenario correctly gated off            |

### `e2e:safe` failures — root cause

All six failures (`dashboard-events.ui`, `dashboard-smoke`, `layout-navigation.ui`,
`monitor-jobs-events.ui` x2, `destructive-actions.mutation`) trip
`assertNoErrorBoundary(page)` finding `getByRole('alert')` count = 1.

The alert is the "Provisioning failed." banner rendered by
[web/src/components/cards/ClusterCard/ClusterCard.tsx](../../../web/src/components/cards/ClusterCard/ClusterCard.tsx#L157)
when `aksApi.recentFailedProvisions(24, 1)` returns a non-empty list.

`scripts/e2e/fixtures/mockApi.ts` and `scripts/e2e/scenarios/helpers/apiMocks.ts`
**do not** stub `**/api/aks/recent-failed-provisions**`, so the real local API
answers. The local jobstate table holds a leftover failed row from 2026-05-24:

```
{
  "job_id": "901fc6b6-…",
  "status": "failed",
  "cluster_name": "aks-elb-e2e-core-nt",
  "region": "eastus2",
  "resource_group": "rg-elb-dashboard",
  "created_at": "2026-05-24T14:00:46+00:00"
}
```

This is a **mock coverage gap**, not a behavioural regression. Suggested
follow-up (one PR): add to both files

```ts
await page.route("**/api/aks/recent-failed-provisions**", (route) =>
  route.fulfill({ status: 200, contentType: "application/json",
    body: JSON.stringify({ jobs: [], degraded: false }) }),
);
```

The validation does not auto-apply this — the skill is read-only on the
codebase except for the change-note itself.

## Deployed health (gate 2)

- `GET /api/health` → `{ status: "ok", revision: "ca-elb-dashboard-01--0000006", app_insights_configured: false }`
- `GET /api/terminal/health` → `{ status: "ok" }`
- `GET /` → SPA shell 200
- `GET /runtime-config.js` → `window.__ELB_RUNTIME_CONFIG__` populated
- Container App revision `0000006` `RunningAtMaxScale`, traffic 100 %, all six
  sidecars (`api`, `worker`, `beat`, `frontend`, `redis`, `terminal`) on tag
  `20260525142909`.

### Regression flagged — `VITE_AUTH_DEV_BYPASS=true` baked into production

The deployed `frontend` container env contains `VITE_AUTH_DEV_BYPASS=true`
and the served `runtime-config.js` reflects it. The bundled SPA therefore
skips MSAL while the `api` sidecar continues to enforce `Authorization:
Bearer` → unauthenticated users will see 401s as soon as any data-plane
call fires.

Root cause: `scripts/dev/quick-deploy.sh::load_simple_env_file()` uses the
`[[ -z "${!key:-}" ]]` guard that treats unset and empty-string identically,
so a value in `web/.env.local` (or the shell where `local-run.sh web` ran
earlier) leaks into `--set-env-vars`. Bicep default is `false`
([infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep#L231))
and `postprovision.sh` forces `false`
([scripts/dev/postprovision.sh](../../../scripts/dev/postprovision.sh#L314)),
so the leak only happens through `quick-deploy.sh`. The same class of bug
was previously documented (2026-05-21) for `VITE_API_BASE_URL`; only that
one variable is currently on the explicit skip-list.

Suggested follow-up (separate PR): change the guard to `[[ -z "${!key+x}" ]]`
**or** widen the skip-list to include every `VITE_AUTH_*` and `AUTH_DEV_BYPASS`
variable.

## Live `azure-core-nt-lifecycle` — `blocked_by_budget`

Per `.github/skills/blast-execution-validation/scenario-matrix.md`, the
lifecycle scenario must skip a fresh full DB prepare when `max-hours` is set
to 4. Readiness check failed on every prerequisite:

| Prerequisite                                                                                  | State                                       |
| --------------------------------------------------------------------------------------------- | ------------------------------------------- |
| AKS cluster present anywhere in subscription `577d6332-…`                                      | `az aks list` returns `[]`                  |
| Workload Storage `stelbdashboard01mul5oh5j` `blast-db` container has prepared `core_nt/` shards | RBAC blocked listing; account created only 2026-05-23, container only 2026-05-25, no worker logs reference `core_nt` prepare task in last 200 lines |
| App Insights configured for telemetry pass                                                     | `APPLICATIONINSIGHTS_CONNECTION_STRING=""`, `ENABLE_APPLICATION_INSIGHTS=false`, `/api/health.app_insights_configured=false` |
| MSAL bearer for deployed `api` (to call `/api/blast/databases`, etc.)                          | AADSTS65001 — interactive admin consent required for `api://ddf48c19-…/.default` |

A fresh lifecycle from zero would need, optimistically: AKS provision
~15–30 min + `core_nt` prepare 4–8 h + shard 1–2 h + warmup 0.5–1 h + BLAST
submit/complete 0.5–1 h, total **≥ 7 h** — outside the 4 h cap. No
"already warm" shortcut exists in this environment.

Decision: report `blocked_by_budget` and surface the regressions above as
the actionable output of this run.

## App Insights pass

The skill's `app-insights-kql.md` workflow is **not applicable** to this
deployment because App Insights is disabled. No KQL evidence to attach.
Recommend enabling App Insights before the next `full-azure` invocation so
the telemetry pass becomes available.

## Operational hygiene

- Local-debug storage open / close pair (`scripts/dev/storage-public-access.sh
  on/off` with explicit `--account/--rg/--subscription`) used once for the
  `blast-db` probe and closed before this note was written
  (`{"bypass":"AzureServices","defaultAction":"Deny","ipRules":[],"public":"Disabled"}`).
- No code changed, no resource deleted, no scope expansion. User WIP not touched.

## Validation evidence summary

```
pytest:        187/187 ✓
vitest:        19/19   ✓
playwright safe:   6 fail (mock gap, not regression)
playwright api-blast preflight: ✓
deployed /api/health:           ✓
deployed /api/terminal/health:  ✓
deployed /runtime-config.js:    ✗ (VITE_AUTH_DEV_BYPASS=true leak)
lifecycle scenario:             blocked_by_budget
```

## Follow-up issues to open (recommended)

1. **mock gap**: stub `/api/aks/recent-failed-provisions` in
   `scripts/e2e/fixtures/mockApi.ts` and
   `scripts/e2e/scenarios/helpers/apiMocks.ts`.
2. **deploy regression**: harden `scripts/dev/quick-deploy.sh
   ::load_simple_env_file()` against `.env.local` leak (guard or
   skip-list `VITE_AUTH_DEV_BYPASS` + `AUTH_DEV_BYPASS`).
3. **observability**: enable App Insights for `ca-elb-dashboard-01` so the
   skill's telemetry pass becomes runnable.
4. **state hygiene**: decide policy for stale `jobstate` rows older than N
   days (the leftover 2026-05-24 row above is functionally fine in prod
   but actively interferes with the `e2e:safe` mock-less paths).
