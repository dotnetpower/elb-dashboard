# 2026-05-14 — Container Apps migration Phase 0: code scaffolding

## Motivation

The migration plan in [docs/container-apps-migration.md](../../container-apps-migration.md)
defines a five-phase rollout from the current Function App + SWA setup to a
single bundled Container App with six sidecars. This change lands **Phase 0**:
all the source code and infrastructure-as-code that the bundled topology
needs, with **no Azure deployment performed**. Production (`rg-elb-prod`,
`func-elb-prod-*`, `kind-coast-*.azurestaticapps.net`) is unchanged.

The user explicitly asked to "마이그레이션 진행해" (proceed with the migration)
while away. Phase 0 is the safe autonomous step: it produces reviewable code
without spending money or touching production. Phases 1–5 are gated by
explicit user approval because they involve `azd provision`, MSAL App
Registration changes, and DNS cutover.

## User-facing change

None at runtime. The new code lives in new directories:

* `api_app/` — FastAPI implementation of the future `api` sidecar.
* `web/Dockerfile`, `web/nginx.conf` — frontend sidecar image inputs (the
  existing Vite SPA in `web/src/` is reused unchanged).
* `infra/modules/containerAppsEnvironment.bicep`,
  `infra/modules/containerAppControl.bicep`,
  `infra/modules/storageState.bicep` — Bicep modules for the new topology.
* `scripts/dev/docker-compose.local.yml` — local 2-sidecar smoke-test.

The existing Function App (`api/`), the existing SWA build of `web/`, and
`infra/main.bicep` + `infra/modules/platform.bicep` are not modified.

## Files added

| Path | Purpose |
|------|---------|
| `api_app/__init__.py` | Package marker, `__version__`. |
| `api_app/main.py` | FastAPI app factory; mounts `/api/health`, `/api/me`, `/api/monitor/*`. Structured-ish JSON logging. |
| `api_app/auth.py` | MSAL bearer-token validator as a FastAPI `Depends()` dependency. Mirrors the JWKS caching strategy of [api/auth/token.py](../../../api/auth/token.py); kept independent so the `api_app/` package has no `azure.functions` import. |
| `api_app/routes/__init__.py` | Sub-routers package. |
| `api_app/routes/health.py` | `GET /api/health` — no auth, returns version + revision id. |
| `api_app/routes/me.py` | `GET /api/me` — auth-required, returns caller `oid`/`tid`/`upn`. |
| `api_app/routes/monitor.py` | `GET /api/monitor/cluster` — auth-required stub. Phase 3 swaps this for the real cluster card backed by `api/services/monitoring.py`. |
| `api_app/requirements.txt` | Pinned: `fastapi==0.115.5`, `uvicorn[standard]==0.32.0`, `httpx==0.27.2`, `pyjwt[crypto]==2.10.0`, `azure-identity==1.19.0`. |
| `api_app/Dockerfile` | Two-stage build (`python:3.11-slim` deps → runtime). Non-root user (uid 10001). HEALTHCHECK on `/api/health`. Two uvicorn workers. |
| `api_app/.dockerignore` | Standard Python ignores. |
| `web/Dockerfile` | Two-stage build (`node:20-alpine` Vite build → `nginx:alpine`). Listens on `:8081` (loopback target for the api reverse proxy). |
| `web/nginx.conf` | Ports `staticwebapp.config.json` security headers, sets immutable cache for `/assets/*`, no-cache for `/index.html`, SPA navigation fallback to `/index.html`. |
| `web/.dockerignore` | Standard Node ignores. |
| `infra/modules/containerAppsEnvironment.bicep` | Workload-profile Container Apps Environment, VNet-integrated via `infrastructureSubnetId`. Wired to a Log Analytics workspace passed by resource id. |
| `infra/modules/storageState.bicep` | Adds children to the existing platform Storage account: tables `jobstate` + `jobhistory`; containers `audit`, `dead-letter`, `job-payloads`, `schedules`; file shares `redis-data` + `terminal-home`; lifecycle policy that cools/deletes audit blobs. |
| `infra/modules/containerAppControl.bicep` | Single `ca-elb-control` Container App. `minReplicas: 1`, `maxReplicas: 1`. Public ingress on the api sidecar at `:8080`. The api sidecar is enabled; the other five sidecars (`frontend`, `worker`, `beat`, `redis`, `terminal`) are documented inline as TODO blocks describing image, resource budget, command, env vars, and volume mounts so phase 2 is a fill-in-the-template. |
| `scripts/dev/docker-compose.local.yml` | Builds and runs the api + frontend sidecars on host ports 8080 and 8081 for local validation. |
| `docs/features_change/2026-05/2026-05-14-container-app-phase0-scaffolding.md` | This document. |

## Validation evidence

```text
$ python -c "from api_app.main import app; ..."
{"ts":"...","level":"INFO","logger":"api_app.main","msg":"api sidecar started, version=0.0.1"}
routes:
  ['GET', 'HEAD'] /openapi.json
  ['GET'] /api/health
  ['GET'] /api/me
  ['GET'] /api/monitor/cluster

$ TestClient + AUTH_DEV_BYPASS=true ...
health: 200 {'status': 'ok', 'version': '0.0.1', 'revision': 'local'}
me (no token): 401 {'detail': 'missing bearer token'}
monitor (no token): 401 {'detail': 'missing bearer token'}
```

```text
$ az bicep build --file infra/modules/containerAppsEnvironment.bicep --stdout > /dev/null
OK
$ az bicep build --file infra/modules/storageState.bicep --stdout > /dev/null
OK
$ az bicep build --file infra/modules/containerAppControl.bicep --stdout > /dev/null
OK
```

## Phase 1 ready-to-go list

To progress to Phase 1 ("Containerize the API on a private network"), an
operator needs to:

1. Decide a target environment (recommended: a fresh `rg-elb-ca-staging` in
   `koreacentral` to avoid colliding with `rg-elb-prod`).
2. Provision the platform VNet + subnets (`snet-containerapps` /23 delegated
   to `Microsoft.App/environments`, `snet-private-endpoints`, `snet-aks`),
   the platform Log Analytics workspace, and the platform ACR (or reuse the
   existing one).
3. Build and push the api image:
   ```bash
   az acr build -r <acr> -t elb-api:phase0 -f api_app/Dockerfile .
   ```
4. Wire `containerAppsEnvironment.bicep` and `containerAppControl.bicep` into
   a new `infra/main.staging.bicep` or extend `infra/main.bicep` behind a
   feature flag, then `azd provision` (separate `azd env`).
5. Verify `GET /api/health` responds on the new ingress hostname.

Until then, this PR is purely additive code and can ship without any Azure
side effects.

## Out of scope (deferred to later phases)

* **Phase 2** — wire the other five sidecars (frontend, worker, beat, redis,
  terminal), build their images, mount the Azure Files volumes, dispatch
  Celery tasks, add the WebSocket terminal proxy.
* **Phase 3** — migrate the route set from `api/` to `api_app/`, delete the
  per-VM terminal API surface, delete cloud-init.
* **Phase 4** — verify and tighten private networking (Key Vault, Storage,
  ACR, AKS, the Azure Files private endpoint that backs the redis mount).
* **Phase 5** — production cutover; delete the SWA and Function App
  resources after a full release window.
