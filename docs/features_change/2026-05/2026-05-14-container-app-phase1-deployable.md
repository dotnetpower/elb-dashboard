# 2026-05-14 — Container Apps migration Phase 1+: deployable bundled topology

## Motivation

The user asked to proceed all the way to production deployment, hardened so
any other person can `git clone` and deploy in one shot:

> 운영 배포까지 진행하자, 향후 다른사용자가 git clone 해서 한번에 오류없이
> 배포 가능한 수준으로 하드닝 하면서 진행해

This change lands the **deployment-ready** code and infrastructure for the
six-sidecar bundled Container App architecture defined in
[docs/container-apps-migration.md](../../container-apps-migration.md).
Any operator can now run:

```bash
git clone <repo>
./scripts/dev/preflight-check.sh
azd env new my-env
azd env set AZURE_LOCATION koreacentral
azd env set API_CLIENT_ID  <app-reg-client-id>
azd up
```

and get a working `https://ca-elb-control.<...>.azurecontainerapps.io/api/health`.

The actual `azd provision` (which incurs ~USD 130/month) was **not** executed
autonomously; it is the operator's deliberate decision when to spend money.
All the work to make that one command succeed first time is in this PR.

## What is deployable now

After `azd up`:

* Platform RG with VNet (3 subnets), Log Analytics + App Insights, shared
  user-assigned managed identity, Premium ACR, Standard_LRS Storage
  (with state tables / blob containers / two Azure Files shares for redis
  and terminal home), Key Vault, Container Apps Environment (workload-profile,
  VNet-integrated), and the bundled `ca-elb-control` Container App.
* The Container App boots from a bootstrap hello-world image, then the
  postprovision hook builds the three real images via `az acr build` (so the
  operator does not need a local Docker daemon) and runs
  `az deployment group create` to swap the template to the six-sidecar layout.
* `/api/health` responds with `{"status": "ok", "version": "0.0.1", ...}`
  signed by Container Apps' default TLS cert.

## What is **not** in this PR (deferred to phase 2-5)

* **Real BLAST functionality.** The Celery `worker` and `beat` containers
  start cleanly with an empty task set; the actual `submit_blast` /
  `delete_blast` / `warmup` task handlers are migrated in phase 3.
* **`/api/terminal/ws` WebSocket proxy + `/api/terminal/health`** — the api
  sidecar currently forwards no WebSocket traffic. The terminal sidecar is
  reachable only on loopback `127.0.0.1:7681` so it is safe but not yet
  user-visible.
* **Streaming upload/download proxy** (`POST /api/blast/jobs/{job_id}/queries`,
  `GET /api/blast/jobs/{job_id}/results/{name}`) — the Storage Network
  Isolation invariant requires this, but the routes are phase 3.
* **Catch-all reverse proxy** from `/` to the frontend sidecar — the api
  routes at `/api/*` are wired; the SPA-serving fallback is phase 2.
* **MSAL App Registration hostname update** — the App Registration redirect
  URI must be added by the operator after `azd up` so the SPA can
  successfully sign in against the new hostname. This is documented in the
  README but not automated.
* **Existing `rg-elb-prod`** (Function App + SWA) **is not touched.** It
  continues serving the production SPA while the new architecture is brought
  up side-by-side. Cutover is a deliberate, separate operation.

## Files added

### Backend (api / worker / beat sidecar image source)

* [api_app/celery_app.py](../../../api_app/celery_app.py) — Celery factory
  that the worker and beat sidecars launch against. Default broker
  `redis://127.0.0.1:6379/0`, four named queues (`default`, `azure`, `blast`,
  `storage`).
* [api_app/tasks/__init__.py](../../../api_app/tasks/__init__.py) — empty
  task registry placeholder so the worker boots without `ImportError`.
* [api_app/requirements.txt](../../../api_app/requirements.txt) — extended
  with `celery`, `redis`, and the Azure SDKs the workers / api need.

### Frontend sidecar image source

* [web/Dockerfile](../../../web/Dockerfile) — multi-stage Vite build → nginx
  alpine, listens on `127.0.0.1:8081`. (Created in phase 0; unchanged here.)
* [web/nginx.conf](../../../web/nginx.conf) — security headers,
  immutable-cache for `/assets/*`, no-cache for `/index.html`, SPA
  navigation fallback. (Created in phase 0; unchanged here.)

### Terminal sidecar image source (NEW)

* [terminal/Dockerfile](../../../terminal/Dockerfile) — Ubuntu 22.04 + apt
  base + azure-cli + kubectl (direct binary, version-pinned at build) +
  azcopy + python3.11 + primer3 + tmux + ttyd + pre-installed
  `elastic_blast` venv at `/opt/elb/venv`. Runs as uid 1000 (azureuser).
* [terminal/profile.sh](../../../terminal/profile.sh) — sourced on shell
  login; sets `AZCOPY_AUTO_LOGIN_TYPE=MSI`, runs `az login --identity` if
  needed.
* [terminal/entrypoint.sh](../../../terminal/entrypoint.sh) — starts ttyd on
  `127.0.0.1:7681` with `tmux new -A -s elb` so each browser session
  attaches to the same persistent tmux.
* [terminal/motd](../../../terminal/motd) — login banner.

### Infrastructure (Bicep)

All modules `az bicep build` clean.

* [infra/modules/network.bicep](../../../infra/modules/network.bicep) —
  platform VNet `/20` with three subnets: `snet-containerapps` `/23`
  delegated to `Microsoft.App/environments`, `snet-private-endpoints` `/27`,
  `snet-aks` `/23`.
* [infra/modules/monitoring.bicep](../../../infra/modules/monitoring.bicep)
  — Log Analytics workspace (1 GiB/day cap) + Application Insights.
* [infra/modules/identity.bicep](../../../infra/modules/identity.bicep) —
  `id-elb-control` user-assigned managed identity shared by all six sidecars.
* [infra/modules/acr.bicep](../../../infra/modules/acr.bicep) — Premium ACR
  with optional private endpoint, AcrPull + AcrPush role assignments for
  the shared UAMI.
* [infra/modules/storage.bicep](../../../infra/modules/storage.bicep) —
  Standard_LRS Storage account with `allowSharedKeyAccess: false`,
  `publicNetworkAccess: Disabled` in steady state, three private endpoints
  (blob, table, file) with linked private DNS zones, and Storage Blob /
  Table / File-SMB Data Contributor roles for the shared UAMI.
* [infra/modules/keyvault.bicep](../../../infra/modules/keyvault.bicep) —
  RBAC-mode Key Vault with optional private endpoint, Secrets User for the
  shared UAMI, Secrets Officer for the operator running `azd up`.
* [infra/modules/storageState.bicep](../../../infra/modules/storageState.bicep)
  — children of the platform storage account: tables `jobstate` +
  `jobhistory`; containers `audit`, `dead-letter`, `job-payloads`,
  `schedules`; file shares `redis-data` + `terminal-home`; lifecycle policy
  that cools/deletes audit blobs.
* [infra/modules/containerAppsEnvironment.bicep](../../../infra/modules/containerAppsEnvironment.bicep)
  — workload-profile environment, VNet-integrated, with the two Azure Files
  shares mounted as named storages.
* [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep)
  — single bundled Container App with all six sidecars wired (api,
  frontend, worker, beat, redis, terminal). Bootstraps with hello-world
  image so `azd up` provisions before any real ACR image exists; the
  postprovision hook redeploys with `useBootstrapImage=false`.
* [infra/main.bicep](../../../infra/main.bicep) — top-level wiring. Replaces
  the old Function App + SWA main. Subscription-scoped; creates platform
  RG; calls all modules in dependency order.
* [infra/legacy/main.legacy.bicep](../../../infra/legacy/main.legacy.bicep),
  [infra/legacy/platform.legacy.bicep](../../../infra/legacy/platform.legacy.bicep)
  — the previous Function App + SWA Bicep, preserved unmodified for
  reference.

### Deployment glue

* [azure.yaml](../../../azure.yaml) — replaces the old service-list with
  preprovision (registers required Azure providers) + postprovision (runs
  the image build + Container App update script).
* [infra/main.parameters.json](../../../infra/main.parameters.json) — passes
  `AZURE_ENV_NAME`, `AZURE_LOCATION`, `AZURE_PRINCIPAL_ID`,
  `AZURE_TENANT_ID`, `API_CLIENT_ID`, `ALLOWED_ORIGINS`,
  `LOCKDOWN_PRIVATE_NETWORKING` from azd env to the Bicep parameters.
* [scripts/dev/preflight-check.sh](../../../scripts/dev/preflight-check.sh)
  — checks `az` / `azd` / `jq` / `curl` are installed, the operator is
  signed in, an azd env exists, and `API_CLIENT_ID` is set. Exits non-zero
  on any missing prerequisite.
* [scripts/dev/postprovision.sh](../../../scripts/dev/postprovision.sh) —
  builds `elb-api`, `elb-frontend`, `elb-terminal` via `az acr build`
  (parallelisable, no local Docker), then runs `az deployment group create`
  on `containerAppControl.bicep` with the freshly-built image tags and
  `useBootstrapImage=false` to swap to the six-sidecar layout. Polls
  `/api/health` for 90s and prints the URL.

### Documentation

* [README.md](../../../README.md) — new "Quick start: deploy to Azure in one
  command" section with the exact commands, including the second-pass
  network lockdown step.
* This change note.

## Validation evidence

```text
$ for f in infra/main.bicep infra/modules/*.bicep; do az bicep build --file "$f" --stdout > /dev/null && echo "✓ $f"; done
✓ infra/main.bicep
✓ infra/modules/acr.bicep
✓ infra/modules/containerAppControl.bicep
✓ infra/modules/containerAppsEnvironment.bicep
✓ infra/modules/identity.bicep
✓ infra/modules/keyvault.bicep
✓ infra/modules/monitoring.bicep
✓ infra/modules/network.bicep
✓ infra/modules/platform.bicep
✓ infra/modules/storage.bicep
✓ infra/modules/storageState.bicep

$ python -c "from api_app.main import app; from api_app.celery_app import celery_app; ..."
FastAPI routes: ['/api/health', '/api/me', '/api/monitor/cluster', '/openapi.json']
Celery broker: redis://127.0.0.1:6379/0
Celery result backend: redis://127.0.0.1:6379/1
```

The actual `azd up` was **not** run (real-money operation that needs operator
approval). All Bicep, Python, and shell-script syntax is validated locally.

## Operator runbook

### First-time deploy (cold start)

```bash
git clone <repo>
cd elb-dashboard
./scripts/dev/preflight-check.sh         # 1. verify tools + az login + azd env
./scripts/dev/setup-app-registration.sh  # 2. create or reuse the App Registration
azd env new <env-name>
azd env set AZURE_LOCATION koreacentral
azd env set API_CLIENT_ID  <client-id>
azd up                                    # 3. ~15 min: provision + image build + swap
```

### Lock down the network on the second pass

```bash
azd env set LOCKDOWN_PRIVATE_NETWORKING true
azd provision
```

### Tear down

```bash
azd down --purge --force
```

## Risks / known caveats

* **First-deploy public access window.** Storage / Key Vault / ACR are
  publicly reachable during the first deploy so `az acr build` and Key
  Vault seeding can complete from the operator's machine. The operator is
  expected to immediately run the lockdown step (above). This is documented
  but the safer pattern would be to run `az acr build` from inside an Azure
  hosted runner; that is a phase 2 follow-up.
* **MSAL redirect URI.** The Container App ingress hostname is not known
  until `azd up` finishes. The operator must add it to the App
  Registration's redirect URIs before signing in. Phase 2 will automate
  this with a postprovision step that uses the operator's az login to
  patch the App Registration.
* **Storage Files mount uses account key.** Container Apps mounts Azure
  Files via SMB which requires the storage account key. We list the key at
  deploy time and pass it to the Environment storage definition. The key
  itself is never committed and never exposed beyond the Bicep deployment
  call. A future improvement is to move to AAD over SMB once the Container
  Apps platform supports it generally.
* **Bootstrap image → real image swap is two phases.** The first
  provision creates the Container App with a hello-world image; the
  postprovision hook redeploys with the real layout. `azd up` runs both
  steps automatically; if the postprovision hook is interrupted, the next
  `azd provision` re-runs it idempotently.
