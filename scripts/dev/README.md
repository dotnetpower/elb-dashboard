# `scripts/dev/` — local dev / debug loop helpers

| Script / file | When to use |
| --- | --- |
| [`docker-compose.local.yml`](./docker-compose.local.yml) | Minimal 2-sidecar (api + frontend) sanity check. Faster boot; no Celery. |
| [`docker-compose.full.yml`](./docker-compose.full.yml) | **Full 6-sidecar mirror of the bundled Container App.** Use this for any debugging that touches Celery, Redis, the terminal exec channel, or cross-sidecar wiring. |
| [`quick-deploy.sh`](./quick-deploy.sh) | One-sidecar image bump on the live Azure Container App. ~30-90 s per cycle vs. 5-10 min for a full Bicep redeploy. |
| [`postprovision.sh`](./postprovision.sh) | Full first-time / structural deploy — sidecar layout, env vars, secrets, probes, scale rules. Run via `azd up` or directly after sourcing `/tmp/azd-env.sh`. |
| [`smoke_api.py`](./smoke_api.py) | HTTP smoke test against a running api sidecar. |
| [`preflight-check.sh`](./preflight-check.sh) | Pre-`azd up` sanity. |
| [`setup-app-registration.sh`](./setup-app-registration.sh) | One-shot Entra ID app registration creation. |

---

## The three-tier debug loop

Running anything in Azure costs minutes. Running anything locally costs seconds.
The full debug loop has three tiers; **always start at the cheapest tier that
can reproduce the bug.**

### Tier 1 — Pure unit tests (~1-3 s)

```bash
uv run pytest -q api/tests
uv run pytest -q api/tests/test_terminal_exec.py     # focused
```

Use for: anything that doesn't need a live HTTP server (sanitisation, auth
caching, image tag dict, terminal exec contract).

### Tier 2 — Local 6-sidecar compose (~30 s first build, ~5 s thereafter)

```bash
docker compose -f scripts/dev/docker-compose.full.yml up --build
```

Then in another terminal:

```bash
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/health/celery               # queue snapshot
curl -XPOST 'http://127.0.0.1:8080/api/health/celery/enqueue-noop?message=hi'
curl http://127.0.0.1:8080/api/health/celery/result/<id>
open http://127.0.0.1:8080/                                # SPA via api proxy
```

`api/` is **bind-mounted** into the api / worker / beat containers and
`uvicorn --reload` watches it — code edits show up live without a rebuild.
SPA edits in `web/src/` reload via vite HMR.

What this **catches** that a remote deploy used to:

- Celery routing trap (default queue vs. typed queues — see repo memory)
- `wait_redis.py` boot order across sidecars
- Route registration order vs. `frontend_proxy` catch-all
- terminal `exec_server` contract (`EXEC_TOKEN`, allowlist, concurrency)
- WebSocket proxy plumbing (`/api/terminal/ws`)
- Reverse proxy headers and SPA fallback
- `AUTH_DEV_BYPASS` short-circuit

What it **does not** catch (still requires Tier 3):

- Managed Identity / `DefaultAzureCredential` token acquisition
- Private-endpoint networking (Storage / KV)
- Container Apps probe semantics
- ACR pull RBAC
- Real Storage Tables / blobs

### Tier 3 — Single-sidecar quick deploy (~1-2 min)

When the bug only reproduces on Azure (MI, private endpoints, real Storage),
do **not** run a full Bicep redeploy unless you actually changed sidecar
structure. Use:

```bash
source /tmp/azd-env.sh                           # or however you populate env
scripts/dev/quick-deploy.sh api --logs           # build + patch + tail logs
scripts/dev/quick-deploy.sh terminal             # only terminal toolchain change
scripts/dev/quick-deploy.sh frontend             # only SPA change
```

`quick-deploy.sh api` automatically patches `worker` and `beat` containers too
because they share the api image — leaving them on a stale tag was a real
source of confusion last week.

For full structural changes (env / secrets / probes / new sidecar) **fall
back to** `postprovision.sh` or `az deployment group create`.

---

## Common pitfalls

- **Don't expose the terminal sidecar's ttyd in compose.** It still binds
  loopback inside the terminal container; the api → terminal hop in compose
  uses `terminal:7682` (exec_server, which honours `EXEC_HOST=0.0.0.0`).
  ttyd browser shell is only reachable through the api WebSocket proxy.
- **Compose uses a fixed dev `EXEC_TOKEN`.** Never reuse this value in any
  Azure deployment. Real deploys mint a fresh GUID via Bicep `newGuid()`.
- **Compose does not wire MI / Storage / KV.** Any code path that hits
  `azure_clients` will fail. That is intentional — those code paths must be
  validated in Tier 3.
- **Don't stop using `postprovision.sh`.** It is still the source of truth
  for sidecar layout. `quick-deploy.sh` is for *image-only* iteration.
