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
| [`install-git-hooks.sh`](./install-git-hooks.sh) | **Install the CI-mirror git hooks** (sets `core.hooksPath=scripts/dev/git-hooks`). Run once per fresh clone. pre-commit = `ruff` + docs frontmatter guard on staged files; pre-push = `pytest` + `mkdocs build --strict` scoped to the pushed paths. Mirrors [`.github/workflows/test.yml`](../../.github/workflows/test.yml) and [`docs.yml`](../../.github/workflows/docs.yml). Bypass with `--no-verify` or `ELB_SKIP_HOOKS=1`. |
| [`git-hooks/`](./git-hooks/) | The hook scripts themselves (`pre-commit`, `pre-push`, shared `_lib.sh`). Edit these when the CI workflows' checks or `paths:` filters change so the local mirror stays accurate. |
| [`setup-app-registration.sh`](./setup-app-registration.sh) | One-shot Entra ID app registration creation. |
| [`grant-local-rbac.sh`](./grant-local-rbac.sh) | One-shot: grant your `az login` user the minimum RBAC (Storage Blob Data Contributor, Storage Account Contributor, RG Reader, AcrPull) needed to drive a deployed environment from a local api sidecar. Idempotent; run once per fresh clone. |
| [`storage-public-access.sh`](./storage-public-access.sh) | Manually flip a workload Storage account's `publicNetworkAccess` on (IP-allowlisted) / off for local debugging. The api also auto-opens it when `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` — see `api/services/storage/public_access.py`. |
| [`local-run.sh`](./local-run.sh) | Direct terminal and VS Code task entrypoint for detached `start` / `stop` / `restart` / `status`, individual services (`api`, `worker`, `beat`, `web`, `redis`, `terminal-exec`), `smoke`, `compose-full`, and `compose-local`; always routes through local logging. |
| [`e2e-ui.sh`](./e2e-ui.sh) | One-command UI E2E session launcher. Starts local api + web in dev-bypass mode without Azure login, or delegates to `auth-on` for real MSAL login, then exports headed/headless scenario environment. |
| [`run-with-log.sh`](./run-with-log.sh) | Lower-level wrapper that mirrors any local dev command's stdout/stderr into `.logs/local/latest/*.log` for warning/error review. |

---

## Local logs

VS Code dev tasks and direct terminal runs through `scripts/dev/local-run.sh`
write project-local logs under **a single fixed location** so failures are
visible from the workspace without relying on terminal scrollback:

```text
.logs/local/
  latest/                   # the only place logs ever land
    api.log
    api.log.1               # rotated chunks (ring, see LOCAL_LOG_MAX_CHUNKS)
    worker.log
    beat.log
    web.log
    redis.log
    terminal-exec.log
    compose-full.log
    compose-full-containers.log
    <service>.launch.log    # detached-launcher stdout for `local-run.sh start`
    <service>.launch.pid
  _archive/                 # legacy session folders or `logs-clean` archives
  api-<port>.lock           # api start lock (flock)
```

There are **no** timestamped session folders, no `latest` symlink, no
`.current-session` marker, no `.lock/` directory. One service → one file →
ring rotation.

Rules:

- one fixed log file per service: `.logs/local/latest/<service>.log`;
- appended across runs so a `restart` does not lose the previous traceback;
- cap each chunk at 1 MiB by default (`LOCAL_LOG_MAX_BYTES=1048576`) and keep
  at most 5 chunks per service in a ring (`LOCAL_LOG_MAX_CHUNKS=5`), so each
  service stays under ~5 MiB on disk no matter how long you debug;
- flush the first few lines immediately, then batch file flushes every 50
  lines (`LOCAL_LOG_FLUSH_LINES=50`) to avoid per-line filesystem pressure;
- keep console output unchanged while mirroring it to files;
- set `LOCAL_LOG_CONSOLE=false` for high-volume runs when terminal rendering is
  the bottleneck and file logs are enough;
- replay only the newest 200 lines when starting a detached Docker Compose log
  follower (`COMPOSE_LOG_TAIL=200`);
- ignore `.logs/` in git.

Inspecting and tidying:

```bash
scripts/dev/local-run.sh logs        # list .logs/local/latest/ contents with sizes
scripts/dev/local-run.sh logs-clean  # move current logs into .logs/local/_archive/<ts>/
tail -f .logs/local/latest/api.log   # always the right file, no symlink chasing
```

`local-run.sh start` also performs a one-shot migration on first run: any
leftover artifacts from the retired session-folder layout (timestamped
`20260515T...` directories, `web-debug`, `log-guarantee-*`, `.current-session`,
etc.) are moved into `.logs/local/_archive/<utc-ts>/` so the active directory
stays clean. Nothing is deleted.

Use `.logs/local/latest/api.log` first when looking for API warnings/errors,
then compare `worker.log`, `beat.log`, and `web.log` to verify the local
pipeline is healthy end to end.

Direct examples:

```bash
scripts/dev/local-run.sh start     # detached host-mode full stack; returns immediately
scripts/dev/local-run.sh restart   # stop, then detached host-mode start
scripts/dev/local-run.sh stop      # stop host-mode services, compose stacks, Redis, and Azurite
scripts/dev/local-run.sh status    # print host-mode service state
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

UI E2E launcher examples:

```bash
scripts/dev/e2e-ui.sh bypass --headless
scripts/dev/e2e-ui.sh bypass --headed
scripts/dev/e2e-ui.sh login --ask-browser
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:dashboard
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:new-search
scripts/dev/e2e-ui.sh bypass --fullstack --headless -- npm --prefix web run e2e:azure-core-nt-lifecycle
```

When no browser flag is supplied, `e2e-ui.sh` asks briefly on interactive
terminals: pressing Enter opens a visible browser, while no response falls back
to headless mode. In CI and non-interactive shells it chooses headless
automatically. Real MSAL login still requires the user to complete Microsoft
sign-in, MFA, or device-code prompts directly. Use `--fullstack` for scenarios
that enqueue Celery work or call the terminal exec sidecar; it starts redis,
api, worker, beat, web, and terminal-exec before running the scenario.

Host-mode API startup keeps `127.0.0.1:8085` stable because the Vite dev
server and smoke scripts expect that port. `local-run.sh api` takes a per-port
startup lock and checks `/api/health` before invoking uvicorn: if the local API
is already healthy, the command exits successfully instead of writing an opaque
`Address already in use` failure; if another process owns the port, the log
prints the listener details from `ss` or `lsof`.

For agent-driven or one-command local sessions, prefer `local-run.sh start` or
the VS Code `server: start` / `fullstack: start` task. Those commands launch
Redis, terminal-exec, api, worker, beat, and web through detached background
processes, then return immediately while service logs continue under
`.logs/local/latest/`. Use `local-run.sh restart` for a fresh server cycle and
`local-run.sh status` when you need readiness details without attaching to the
long-running processes.

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

#### Incremental loop with testmon (sub-second reruns)

When the full ~30 s suite is too slow for a tight edit-test cycle, use
[`test-inc.sh`](./test-inc.sh) — it runs **only** the tests whose covered code
changed in your working tree (via `pytest-testmon`):

```bash
scripts/dev/test-inc.sh                    # whole suite, incremental
scripts/dev/test-inc.sh api/tests/test_foo.py   # scope the coverage map
ELB_TESTMON_RESET=1 scripts/dev/test-inc.sh     # rebuild the .testmondata map
```

The first run builds a git-ignored `.testmondata` coverage map (one full run);
every later run deselects unaffected tests automatically ("N deselected /
K selected" in <1 s). testmon uses AST-level fingerprints, so comment/whitespace
edits do not trigger reruns. The wrapper clears `pytest.ini`'s addopts because
testmon silently disables itself under `-m` (marker exclusion) and is
incompatible with `-n auto` (xdist). **This is a local convenience only** — CI
and the pre-push hook still run the full `uv run pytest -q api/tests`.

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
