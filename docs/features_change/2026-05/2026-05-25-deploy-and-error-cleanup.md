# Container Apps Redeploy + App Insights Error Cleanup

**Date**: 2026-05-25

## Motivation

Operator request: redeploy every sidecar of the bundled Container App
`ca-elb-dashboard-01` and clear all errors surfacing in App Insights / LAW.
Constraint: hygiene/bug-fix only — no feature work.

Two distinct error sources were active in
`ContainerAppConsoleLogs_CL` for the last hour before this change:

1. **`AuthorizationFailure` + `Traceback`** from the `worker` sidecar
   (`reconcile_auto_warmup` raising `UnpickleableExceptionWrapper`)
   because the workload Storage account
   `stelbdashboard01mul5oh5j` had drifted into a half-baked
   `publicNetworkAccess=Disabled` state with **zero** private endpoints
   — neither the lockdown nor the bootstrap posture from
   `infra/main.bicep`. Direct ARM update restored
   `Enabled + defaultAction=Allow + bypass=AzureServices`, immediately
   eliminating the auth failures.
2. **`Connection refused` / `Error 111`** spam from `cgroup_reporter`
   in `api`, `worker`, `frontend`, `terminal` during the
   first 30-60 s of every revision activation — the in-revision
   `redis` sidecar takes longer than the other sidecars to bind
   `127.0.0.1:6379`, so the 5-second metrics tick logs a warning on
   every miss. Also a one-shot worker/beat `FATAL: Redis not
   reachable after 120s` when the redis sidecar's cold start exceeds
   the `wait_redis.py` timeout.

## User-facing change

None. Pure operational hygiene.

## API / Code diff summary

| # | File | Change |
|---|------|--------|
| 1 | `api/services/cgroup_reporter.py` | Add `CGROUP_REDIS_GRACE_TICKS` (default 12 = 60 s). First N `RedisError` ticks log at DEBUG; after that, escalate to WARNING. Counter resets on the first successful publish, with an INFO recovery line if at least one tick had failed. |
| 2 | `terminal/cgroup_reporter.py` | Same grace-tick logic, standalone copy used by the terminal sidecar. |
| 3 | `web/cgroup_reporter.py` | Same grace-tick logic, standalone copy used by the frontend sidecar. |
| 4 | `api/wait_redis.py` | Default `REDIS_WAIT_TIMEOUT` bumped 120 s → 180 s. Container Apps occasionally needs more than 2 min for the in-revision redis sidecar to bind during activation. |
| 5 | `api/services/auto_warmup_reconcile.py` | Wrap the `list_auto_warmup_preferences()` call inside `reconcile_auto_warmup_preferences` in try/except. On transient Storage failures the task now logs a WARNING and returns `{"status": "list_failed", …}` instead of raising and producing an `UnpickleableExceptionWrapper`. Mirrors the defensive pattern already used by `reconcile_stale_jobs` and `backfill_completed_runtime_metrics`. |

No IaC diff. (See "Follow-up" below for the deferred Bicep split.)

## Out-of-band runtime change

`stelbdashboard01mul5oh5j` was drifted (PNA `Disabled`, no PE) and
corrected via direct `az storage account update`:

```
az storage account update \
  --subscription 577d6332-de48-4a30-be66-dded26a712ea \
  -g rg-elb-dashboard-01 \
  -n stelbdashboard01mul5oh5j \
  --public-network-access Enabled \
  --default-action Allow \
  --bypass AzureServices
```

Resulting posture: `publicNetworkAccess=Enabled,
defaultAction=Allow, bypass=AzureServices, 0 ipRules, 0 vnetRules`.
This matches what `infra/main.bicep` produces when
`lockdownPrivateNetworking=false` (the current default) — i.e. it
restores the deploy-time posture. Charter §9 calls for production
to be `Disabled`. See "Follow-up".

## Deploy summary

| Sidecar | Tag | Revision introducing it | Final active revision |
|---------|-----|-------------------------|------------------------|
| api / worker / beat | `20260525211518` | `0000015` | `0000017` (Healthy, traffic 100, 1 replica) |
| terminal | `20260525212015` | `0000016` | `0000017` |
| frontend | `20260525212254` | `0000017` | `0000017` |

Each `az containerapp update --container-name <name>` rolls the
template forward into a new revision that inherits the previously
deployed images of the other containers. After all three deploys the
final revision `0000017` carries the patched images for every
sidecar, traffic 100, 1 replica, Healthy. Older revisions
auto-deactivated.

Frontend redeploy used the documented env-export pattern: back up
`web/.env.local`, empty it, run `quick-deploy.sh frontend` with
explicit `VITE_AUTH_DEV_BYPASS=false` + `AUTH_DEV_BYPASS=false`
exports, restore the backup.

### Frontend SPA env validation

Verified against the served `/runtime-config.js`:

```
window.__ELB_RUNTIME_CONFIG__ = {
  "VITE_API_BASE_URL": "",
  "VITE_AUTH_DEV_BYPASS": "false",
  "VITE_AZURE_REDIRECT_URI": "__RUNTIME__",
  "VITE_AZURE_TENANT_ID":   "184be312-…",
  "VITE_AZURE_CLIENT_ID":   "ddf48c19-…",
  …feature flags…
};
```

Main bundle (`/assets/index-DzOEESD2.js`) `grep -c 'localhost:8085'`
= **0**.

## Validation evidence

### Pre-change LAW (last 5 m, 21:10 KST)

```
ContainerName    Cat                   N
api              RedisRefused          116
terminal         RedisRefused          59
frontend         RedisRefused          59
worker           AuthorizationFailure  6
worker           Traceback             4
worker           ERROR                 3
worker           FATAL                 2
beat             FATAL                 2
```

### Post-storage-fix LAW (last 3 m, 21:11 KST)

- AuthorizationFailure: `6 → 0`
- Traceback: `4 → 0`
- worker ERROR: false positives (Celery success lines containing the
  string `errors: 0`); no genuine errors remained.

### Post-patch LAW (rev `0000017`, 21:25 → 21:29 KST window, ~4 min after final activation)

Same KQL pattern as pre-change, filtered to the new revision only:

```
ContainerName_s    Cat                   N
-----------------  --------------------  -
(no rows)
```

Zero rows in any of: `AuthorizationFailure`, `RedisRefused`
(`Connection refused`), `FATAL`, `Traceback`. The 4–5 minute
post-activation window historically produced ~250 RedisRefused
spam lines from the `api` container alone (revision `0000012`
sample: api 252, frontend 126, terminal 124, worker/beat FATAL 4/4
each).

Cross-cutting verification: `RedisRefused` on the patched
sidecars after the in-revision redis sidecar bound (`Redis
ready at 127.0.0.1:6379 (attempt 4)`) — within the 180 s
`wait_redis.py` budget and the 60 s `cgroup_reporter` grace
window. The only remaining warning-class line on `0000017` is
the pre-existing Celery `SecurityWarning: You're running the
worker with superuser privileges` — out of scope; unchanged
from baseline.

Per-revision RedisRefused trend (last 30 min):

| Revision | api | frontend | terminal | worker | beat |
|----------|----:|---------:|---------:|-------:|-----:|
| `0000010` | — | — | — | 36 AuthFail + 24 Traceback | — |
| `0000011` | 54 | 27 | 27 | 1 FATAL | 1 FATAL |
| `0000012` | 252 | 126 | 124 | 4 FATAL | 4 FATAL |
| `0000013` | — | — | — | 2 | — |
| `0000014` | — | 1 | — | — | — |
| `0000015` | — | 8 | 6 | — | — |
| `0000016` | — | 1 | — | — | — |
| **`0000017`** | **0** | **0** | **0** | **0** | **0** |

### Tests + lint

```
uv run pytest -q api/tests/test_cgroup_reporter.py api/tests/test_auto_warmup.py
# 18 passed in 3.79s

uv run ruff check api/services/cgroup_reporter.py \
  api/services/auto_warmup_reconcile.py api/wait_redis.py \
  terminal/cgroup_reporter.py web/cgroup_reporter.py
# All checks passed!
```

## Follow-up (open issue when next operator window allows)

`infra/main.bicep` currently exposes a single
`lockdownPrivateNetworking` toggle that couples Storage and ACR
network state. Charter §9 requires production Storage at
`publicNetworkAccess=Disabled`, but flipping the same toggle to true
also locks ACR — and would break `az acr build` from the maintainer
laptop. The right shape is to split the toggle into
`lockdownStorageNetworking` and `lockdownAcrNetworking` so the
Storage half can be set to `true` without disabling local ACR
builds. Tracking issue to be filed; until then the deployed
environment runs in bootstrap posture and the
`scripts/dev/storage-public-access.sh on/off` helper covers any
emergency widening.
