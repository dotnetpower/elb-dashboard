# `scripts/dev/` — local dev / debug loop helpers

| Script / file | When to use |
| --- | --- |
| [`docker-compose.local.yml`](./docker-compose.local.yml) | Minimal 2-sidecar (api + frontend) sanity check. Faster boot; no Celery. |
| [`docker-compose.full.yml`](./docker-compose.full.yml) | **Full 6-sidecar mirror of the bundled Container App.** Use this for any debugging that touches Celery, Redis, the terminal exec channel, or cross-sidecar wiring. |
| [`compose-with-log.sh`](./compose-with-log.sh) | Docker Compose wrapper used by `local-run.sh compose-full/compose-local`; captures compose output and detached container logs. |
| [`quick-deploy.sh`](./quick-deploy.sh) | One-sidecar image bump on the live Azure Container App. ~30-90 s per cycle vs. 5-10 min for a full Bicep redeploy. Terminal deploys reuse a content-hashed `elb-terminal-base` toolchain image. |
| [`cli-upgrade.sh`](./cli-upgrade.sh) | Safe `git pull` + build + rolling-update **envelope** around `quick-deploy.sh` / `postprovision.sh`. Snapshots the current revision's image refs before any PATCH, polls `/api/health`, and auto-rolls back on failure. Also has a `rollback` mode for emergency recovery. Full guide: [docs/operate/cli-upgrade.md](../../docs/operate/cli-upgrade.md). |
| [`postprovision.sh`](./postprovision.sh) | Full first-time / structural deploy — sidecar layout, env vars, secrets, probes, scale rules. Run via `azd up` or directly after sourcing `/tmp/azd-env.sh`. |
| [`smoke_api.py`](./smoke_api.py) | HTTP smoke test against a running api sidecar. |
| [`preflight-check.sh`](./preflight-check.sh) | Pre-`azd up` sanity. |
| [`setup-app-registration.sh`](./setup-app-registration.sh) | One-shot Entra ID app registration creation. |
| [`grant-local-rbac.sh`](./grant-local-rbac.sh) | One-shot: grant your `az login` user the minimum RBAC (Storage Blob Data Contributor, Storage Account Contributor, RG Reader, AcrPull) needed to drive a deployed environment from a local api sidecar. Idempotent; run once per fresh clone. |
| [`storage-public-access.sh`](./storage-public-access.sh) | Manually flip a workload Storage account's `publicNetworkAccess` on (IP-allowlisted) / off for local debugging. The api also auto-opens it when `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` — see `api/services/storage_public_access.py`. |
| [`local-run.sh`](./local-run.sh) | Direct terminal and VS Code task entrypoint for `api`, `worker`, `beat`, `web`, `redis`, `smoke`, `compose-full`, and `compose-local`; always routes through local logging. |
| [`run-with-log.sh`](./run-with-log.sh) | Lower-level wrapper that mirrors any local dev command's stdout/stderr into `.logs/local/latest/*.log` for warning/error review. |

---

## Local logs

VS Code dev tasks and direct terminal runs through `scripts/dev/local-run.sh`
write project-local logs under `.logs/local/` so failures are visible from the
workspace without relying on terminal scrollback:

```text
.logs/local/
  latest -> 20260515T143012Z-12345
  20260515T143012Z-12345/
    api.log
    worker.log
    beat.log
    web.log
    redis.log
    smoke.log
    compose-full.log
    compose-full-containers.log
```

Rules:

- keep the newest 3 log sessions;
- cap each log chunk at 1 MiB by default (`LOCAL_LOG_MAX_BYTES=1048576`);
- keep at most 16 chunks per service in a session (`LOCAL_LOG_MAX_CHUNKS=16`),
  rotating as a bounded ring so long-running debug sessions cannot grow
  without limit;
- flush the first few lines immediately, then batch file flushes every 50 lines
  (`LOCAL_LOG_FLUSH_LINES=50`) to avoid per-line filesystem pressure;
- keep console output unchanged while mirroring it to files;
- set `LOCAL_LOG_CONSOLE=false` for high-volume runs when terminal rendering is
  the bottleneck and file logs are enough;
- reuse one freshly-created session for parallel task startup
  (`LOCAL_LOG_SESSION_TTL_SECONDS=120`);
- reject unsafe `LOCAL_LOG_SESSION` names and recover stale lock directories
  (`LOCAL_LOG_LOCK_STALE_SECONDS=30`) so logging cannot hang future starts;
- replay only the newest 200 lines when starting a detached Docker Compose log
  follower (`COMPOSE_LOG_TAIL=200`);
- ignore `.logs/` in git.

Use `.logs/local/latest/api.log` first when looking for API warnings/errors,
then compare `worker.log`, `beat.log`, and `web.log` to verify the local
pipeline is healthy end to end.

Direct examples:

```bash
scripts/dev/local-run.sh api
scripts/dev/local-run.sh web
scripts/dev/local-run.sh worker
scripts/dev/local-run.sh beat
scripts/dev/local-run.sh redis
scripts/dev/local-run.sh smoke
scripts/dev/local-run.sh compose-full -- up --build
scripts/dev/local-run.sh compose-full -- up -d --build
scripts/dev/local-run.sh compose-local -- up --build
scripts/dev/local-run.sh compose-local -- up -d --build
```

Host-mode API startup keeps `127.0.0.1:8085` stable because the Vite dev
server and smoke scripts expect that port. `local-run.sh api` takes a per-port
startup lock and checks `/api/health` before invoking uvicorn: if the local API
is already healthy, the command exits successfully instead of writing an opaque
`Address already in use` failure; if another process owns the port, the log
prints the listener details from `ss` or `lsof`.

Docker Compose logging:

- foreground `compose-full -- up --build` writes `compose-full.log`;
- detached `compose-full -- up -d --build` writes the command output to
  `compose-full.log` and starts a background follower writing container output
  to `compose-full-containers.log`;
- `compose-local` uses the same pattern with `compose-local.log` and
  `compose-local-containers.log`.
- detached followers use `docker compose logs -f --tail ${COMPOSE_LOG_TAIL:-200}`
  so an old noisy container cannot replay an unbounded backlog into the local
  log pipeline.
- starting a new detached compose run cleans up stale followers for the same
  compose profile; `compose-full -- down|stop|rm` also stops its follower.

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
scripts/dev/local-run.sh compose-full -- up --build
```

For detached compose runs, use:

```bash
scripts/dev/local-run.sh compose-full -- up -d --build
```

This starts a background log follower. Check
`.logs/local/latest/compose-full-containers.log` for the container stream.

Then in another terminal:

```bash
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/health/celery               # queue snapshot
curl -XPOST 'http://127.0.0.1:18080/api/health/celery/enqueue-noop?message=hi'
curl http://127.0.0.1:18080/api/health/celery/result/<id>
open http://127.0.0.1:18080/                                # SPA via api proxy
```

> The compose api binds **18080** on the host, not 8085, to avoid clashing
> with the workspace `api: start` task (8085) and `web: dev` task (8090).
> If 18080 is also taken on your machine, change the host-side port in
> `docker-compose.full.yml`.

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
scripts/dev/quick-deploy.sh terminal             # terminal runtime/script change
scripts/dev/quick-deploy.sh terminal --rebuild-terminal-base  # force heavy terminal toolchain rebuild
scripts/dev/quick-deploy.sh frontend             # only SPA change
```

`quick-deploy.sh api` automatically patches `worker` and `beat` containers too
because they share the api image — leaving them on a stale tag was a real
source of confusion last week.

Terminal deploys are split into a heavy base and a thin runtime overlay. The
base is tagged from `terminal/Dockerfile.base`, `patch_elastic_blast.py`, and
`merge-sharded-results.sh`; normal terminal deploys reuse it and rebuild only
the runtime scripts from `terminal/Dockerfile.runtime`. Use
`--rebuild-terminal-base` when changing the installed toolchain, pinned tool
versions, or the patched elastic-blast package.

For full structural changes (env / secrets / probes / new sidecar) **fall
back to** `postprovision.sh` or `az deployment group create`.

---

## Common pitfalls

- **WSL DNS**: WSL hosts ship a resolver at `10.255.255.254` that the
  default Docker bridge cannot reach. The compose file works around this
  with `dns: [8.8.8.8, 1.1.1.1]` per service and `build.network: host` on
  every build block. If you copy this compose to a non-WSL host you can
  drop both, but they are harmless if left in place.
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
