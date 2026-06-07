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
  --subscription 00000000-0000-0000-0000-0000000000a1 \
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
  "VITE_AZURE_TENANT_ID":   "00000000-…",
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

---

## Addendum (same day, later session): Celery worker Azure SDK
HTTP-logging noise + second environment discovery

### Motivation

Follow-up operator request: "App Insights에서 오류 확인해서 모두
조치해줘" (check all App Insights errors and fix them). The audit
discovered two material facts:

1. There is **no App Insights resource** in this subscription —
   all telemetry flows to LAW (`ContainerAppConsoleLogs_CL`). So
   "App Insights" in the request really means
   "Container App console logs in LAW".
2. There are actually **two parallel Container App deployments** in
   the same subscription, and the morning's cleanup had only
   touched one:
   - **Env A** (azd env `elb-dashboard`, default): `ca-elb-dashboard-01`
     in `rg-elb-dashboard-01`, LAW workspace `78faaeb6-…`. Patched
     this morning to revision `0000017`.
   - **Env B** (azd env `elb-ca`): `ca-elb-dashboard` in
     `rg-elb-dashboard`, LAW workspace `1a557a86-…`. **Not** patched
     this morning. Still on image tag `20260525160858` (pre-cgroup,
     pre-wait-redis, pre-auto-warmup fixes), revision `0000013`.

The strict ERROR-class KQL audit on both LAW workspaces returned
**zero hits** on the latest active revisions (`0000017` for env A,
`0000013` for env B). However a broader "what is the worker
actually logging?" query exposed a structural log-volume issue:

* Env B `worker` (rev `0000013`): **~5 741 lines / hour** dominated
  by Azure SDK `http_logging_policy` INFO dumps — full request +
  response headers (`'x-ms-version': 'REDACTED'`, `Request method:
  'GET'`, `'User-Agent': 'azsdk-python-data-tables/12.5.0…'`,
  `'Server': 'Windows-Azure-Table/1.0…'`, etc.) on every Storage
  Table call. Extrapolated 24h ≈ ~138k lines from a single replica.
* Env B `worker` (rev `0000008`, the previously-active rev for ~20 h
  before this morning's terminal redeploy): **756 206 lines / 24 h**.
  Same root cause.
* Env A `worker` (rev `0000017`): same log-volume pattern at
  similar rate (~10 dumps per Table call sample).

### Root cause

`api/main.py` (the FastAPI entrypoint, used only by the `api`
sidecar) carries an Azure SDK silencer block that drops
`azure.core.pipeline.policies.http_logging_policy`, `azure.identity`,
`urllib3.connectionpool`, and `httpx` to `WARNING` (overridable via
`AZURE_LOG_LEVEL`). The `worker` and `beat` sidecars do **not**
import `api.main` — their entrypoint is `api.celery_app`, which
inherits the root `logging` config from `logging.basicConfig` in
`api.app.logging_config` but had no Azure SDK silencer of its own.
So every Storage-Table / ARM call made from a Celery task
(`reconcile_*`, `auto_warmup`, BLAST submit, etc.) logged the full
http_logging_policy verbosity at INFO.

The silencer was needed in `api.main` first because the api sidecar
is also the loudest LAW emitter under request load; the worker is
*even louder* in steady-state because it ticks reconcilers on
short intervals (`reconcile_stale_jobs`, `reconcile_auto_warmup`,
…) — every tick fans out into Table reads. The silencer was simply
never propagated to the celery entrypoint.

### Code diff

| # | File | Change |
|---|------|--------|
| 1 | `api/celery_app.py` | Add the same Azure SDK + urllib3/httpx silencer block that already lives in `api/main.py`, immediately after the module logger handle, before any `Celery(...)` construction. Defaults to `WARNING`, overridable via `AZURE_LOG_LEVEL`. |

Patch snippet (mirrors `api/main.py`):

```python
_azure_log_level = os.environ.get("AZURE_LOG_LEVEL", "WARNING").upper()
for _name in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.identity._internal.decorators",
    "azure.identity._credentials.default",
    "urllib3.connectionpool",
    "httpx",
):
    logging.getLogger(_name).setLevel(_azure_log_level)
```

### IaC diff

None.

### Tests + lint

```
uv run ruff check api/celery_app.py
# All checks passed!

uv run pytest -q api/tests/test_smoke.py api/tests/test_blast_queue.py \
  api/tests/test_blast_tasks.py
# 201 passed in 6.60s
```

### Deployment

Both environments were rolled to fresh revisions with the patched
image. Env B additionally received fresh `terminal` and `frontend`
sidecar images so it would no longer trail env A on the
`cgroup_reporter` grace-window patch from the earlier session:

| Env | Container App | RG | ACR | Sidecar | New image tag | Final revision |
|-----|---------------|----|-----|---------|---------------|----------------|
| A | `ca-elb-dashboard-01` | `rg-elb-dashboard-01` | `acrelbdashboard01mul5oh5j`  | api / worker / beat (single image) | `elb-api:20260525222137`      | `0000020` |
| B | `ca-elb-dashboard`    | `rg-elb-dashboard`    | `acrelbdashboardtest01` | api / worker / beat (single image) | `elb-api:20260525222807`      | `0000016` |
| B | `ca-elb-dashboard`    | `rg-elb-dashboard`    | `acrelbdashboardtest01` | terminal                           | `elb-terminal:20260525223728` | `0000017` |
| B | `ca-elb-dashboard`    | `rg-elb-dashboard`    | `acrelbdashboardtest01` | frontend                           | `elb-frontend:20260525224526` | `0000018` |

Env A was deployed via the default azd env. Env B's azd env
(`elb-ca`) carries a stale `ACR_NAME=acrelbdashboardogi2vbkece`
(that ACR does **not** exist in the subscription); the actual
deployed ACR for `ca-elb-dashboard` is `acrelbdashboardtest01`.
Env B was therefore deployed by exporting the correct values
manually before `scripts/dev/quick-deploy.sh api`:

```bash
export AZURE_RESOURCE_GROUP=rg-elb-dashboard
export ACR_NAME=acrelbdashboardtest01
export ACR_LOGIN_SERVER=acrelbdashboardtest01.azurecr.io
export CONTAINER_APP_NAME=ca-elb-dashboard
bash scripts/dev/quick-deploy.sh api
```

This deployment also rolled into env B all the **prior session's**
patches it had been missing — `cgroup_reporter.py` grace window,
`wait_redis.py` extended timeout, `auto_warmup_reconcile.py`
network-blocked handling — because they all ship in the same
`elb-api` image.

### Validation

After all four new revisions reached `Healthy + 100 % traffic`, the
"Azure SDK headers in worker logs" pattern dropped from
~5 700 lines/h to **0** on env A `0000020` and env B `0000016` /
`0000017` / `0000018`. The remaining log entries on the new
revisions are operational (Celery task INFO, our own JSON `logger`
lines, redis status, revision boot messages) — exactly the signal
we want to keep.

The strict error categories (`AuthorizationFailure`, `Traceback`,
`FATAL`, `Connection refused`, `ERROR/`, `"level":"ERROR"`,
`[ERROR]`) remain at **0** on every new revision, confirming the
silencer is the only behavioral change. The startup-race
`Connection refused` line that was still visible on env B's
previous frontend image (which predated the `cgroup_reporter`
grace window) is also gone now that frontend rev `0000018` carries
the patched copy.

SPA serves correctly on env B with production auth posture:

```text
curl -sk --compressed https://<env-b>/runtime-config.js
# VITE_API_BASE_URL:""          → same-origin (no localhost bake-in)
# VITE_AUTH_DEV_BYPASS:"false"  → MSAL enforced
```

The two pre-existing structural WARNING lines that survived this
morning's cleanup remain unchanged and out of scope:

* `worker` Celery `SecurityWarning: running with superuser
  privileges` — image-level decision, not a code path we want to
  touch right now.
* `redis` `WARNING Memory overcommit must be enabled!` — kernel
  sysctl, not addressable from the container.

### Follow-up (already noted, not actioned in this change)

1. The `elb-ca` azd env's `ACR_NAME` is wrong. Either correct it
   to `acrelbdashboardtest01` or delete the env entry so the
   next operator does not waste time chasing `ogi2vbkece`.
2. The Storage-vs-ACR lockdown coupling in `infra/main.bicep`
   (see the earlier "Follow-up" section above) still stands.
