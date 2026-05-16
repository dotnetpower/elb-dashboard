# AGENTS.md — Navigation index for AI agents

> **Policy lives in [.github/copilot-instructions.md](./.github/copilot-instructions.md).**
> This file is the *map* — start here to find the right code fast, then read the
> charter for *how* to change it.

---

## TL;DR for a fresh session

1. **Active backend** is [`api/`](./api/) (FastAPI + Celery, Python 3.12, uv-managed).
   The retired Azure Functions tree lives at [`legacy/functionapp/`](./legacy/functionapp/) — read-only reference.
2. **Active deploy target** is one Azure Container App `ca-elb-control` with
   six sidecars. Bicep entry point: [`infra/main.bicep`](./infra/main.bicep).
3. **Local bring-up**:
   ```bash
   uv sync --all-groups          # creates .venv on Python 3.12
   uv run pytest -q api/tests    # 100 passing
   scripts/dev/local-run.sh api  # writes .logs/local/latest/api.log
   ```
   When starting local servers directly from a terminal, use
   `scripts/dev/local-run.sh <api|worker|beat|web|redis|smoke|compose-full|compose-local>`
   instead of raw `uvicorn`, `celery`, `npm run dev`, or `docker compose` so
   `.logs/local/latest/*.log` is always created for warning/error review.
4. **Never** `pip install`, `func start`, or write a `requirements.txt`. See `.github/copilot-instructions.md` §11.

---

## Where to read first

| Goal | Open this | Then |
|------|-----------|------|
| Add or change a `/api/*` route | [api/main.py](./api/main.py) (route registration) → existing router in [api/routes/](./api/routes/) | Wire its typed client in [web/src/api/endpoints.ts](./web/src/api/endpoints.ts) |
| Add a long-running task | [api/celery_app.py](./api/celery_app.py) → new module under [api/tasks/](./api/tasks/) | Enqueue from a route + persist progress via [api/services/state_repo.py](./api/services/state_repo.py) |
| Touch Azure SDK | [api/services/azure_clients.py](./api/services/azure_clients.py) (the only place that imports `azure.mgmt.*`) | Wrap, then import from a route/task |
| Change image tags | [api/services/image_tags.py](./api/services/image_tags.py) `IMAGE_TAGS` dict | Cross-check against `dotnetpower/elastic-blast-azure` `src/elastic_blast/constants.py` |
| Change Container App layout | [infra/modules/containerAppControl.bicep](./infra/modules/containerAppControl.bicep) | Re-run [scripts/dev/postprovision.sh](./scripts/dev/postprovision.sh) for the template diff |
| Change SPA dashboard cards | [web/src/pages/Dashboard.tsx](./web/src/pages/Dashboard.tsx) + [web/src/api/endpoints.ts](./web/src/api/endpoints.ts) | Add the matching backend route in [api/routes/monitor.py](./api/routes/monitor.py) |
| Update browser terminal | [terminal/Dockerfile](./terminal/Dockerfile) + [terminal/entrypoint.sh](./terminal/entrypoint.sh) (ttyd loopback `127.0.0.1:7681`) | WebSocket proxy in [api/routes/terminal_ws.py](./api/routes/terminal_ws.py) |
| Change auth/JWT validation | [api/auth.py](./api/auth.py) | Tests in [api/tests/test_smoke.py](./api/tests/test_smoke.py) |

---

## Backend route map (what is real vs stub)

Routers are wired in [api/main.py](./api/main.py#L84-L99). Stub routers return
HTTP 503 / 410 by design until their Celery tasks land.

| Prefix | Module | State |
|--------|--------|-------|
| `/api/health` | [api/routes/health.py](./api/routes/health.py) | real |
| `/api/me` | [api/routes/me.py](./api/routes/me.py) | real (MSAL bearer required unless `AUTH_DEV_BYPASS=true`) |
| `/api/monitor/*` | [api/routes/monitor.py](./api/routes/monitor.py) | real (read-only, never 500 — uses `_graceful` to degrade to empty payloads) |
| `/api/arm/*` | [api/routes/arm.py](./api/routes/arm.py) | real (ARM proxy under shared MI) |
| `/api/resources/*` | [api/routes/resources.py](./api/routes/resources.py) | real (synchronous wizard provisioning) |
| `/api/terminal/ws` | [api/routes/terminal_ws.py](./api/routes/terminal_ws.py) | real (WebSocket → loopback ttyd) |
| `/api/terminal/{vm}/...` | [api/routes/terminal_legacy.py](./api/routes/terminal_legacy.py) | **410 Gone** by design — the VM model is retired |
| `/api/aks/*`, `/api/blast/*`, `/api/warmup/*`, `/api/audit/*` | [api/routes/stubs.py](./api/routes/stubs.py) | **503** placeholders awaiting Celery tasks |
| catch-all `/*` (non-`/api/*`) | [api/routes/frontend_proxy.py](./api/routes/frontend_proxy.py) | reverse proxies to frontend sidecar at `127.0.0.1:8081` |

> **Order matters.** [api/main.py](./api/main.py) registers `/api/*` routers
> *before* the frontend catch-all. Adding a new prefix? Insert it above the
> `frontend_proxy` line.

---

## Backend module map (`api/`)

```
api/
├── main.py              # FastAPI app factory + middleware (RequestId, structured logs)
├── celery_app.py        # Celery app + queue routing (azure / blast / storage / default)
├── auth.py              # MSAL bearer-token validation (OIDC discovery + JWKS cache)
├── conftest.py          # pytest sys.path bootstrap
├── routes/              # FastAPI routers (one file per /api/<area>)
├── services/            # The ONLY place that touches azure.mgmt.* / azure.identity / k8s
│   ├── azure_clients.py # Cached Azure SDK client factories (MI under DefaultAzureCredential)
│   ├── monitoring.py    # AKS / Storage / ACR readers + k8s_* kubelet helpers
│   ├── network.py       # ensure_resource_group + VNet/Subnet/NSG primitives
│   ├── compute.py       # VM lifecycle helpers (only used by legacy code paths now)
│   ├── keyvault.py      # KV secret read/write
│   ├── storage_data.py  # Blob upload/list/read — NO SAS issuance, see §9 footgun note
│   ├── state_repo.py    # JobStateRepository (Table Storage + append-blob audit)
│   ├── sanitise.py      # Output redactor (SAS, bearer, sub-id, secrets) — apply at every UI boundary
│   ├── passwords.py     # generate_admin_password (used only by legacy, kept for tests)
│   ├── ssh_exec.py      # paramiko helpers (used only by legacy)
│   ├── blast_config.py  # Azure SKU / pricing constants
│   └── image_tags.py    # IMAGE_TAGS dict — bump in sync with sibling repo
├── tasks/               # Celery tasks live here (mostly empty; populate as you implement stub routes)
└── tests/               # `uv run pytest -q api/tests` → 56 passing
```

---

## Frontend module map (`web/src/`)

```
web/src/
├── App.tsx, main.tsx    # Router + MSAL provider wiring
├── api/
│   ├── client.ts        # fetch wrapper that injects MSAL bearer
│   ├── endpoints.ts     # Typed endpoint helpers (one source of /api/* surface)
│   ├── arm.ts           # Direct ARM token flow (used only where SPA needs subscription list)
│   ├── callerIp.ts      # Browser → ipify (caller IP capture)
│   └── resilience.ts    # Retry / backoff helpers + tests
├── auth/                # MSAL configuration
├── components/          # Reusable glass cards, modals, etc.
├── pages/               # Dashboard, BlastJobs, BlastResults, BlastAnalytics, RemoteTerminal, ...
├── hooks/, data/, theme/, constants.ts
```

UI tokens (glassmorphism) are CSS variables; see [.github/copilot-instructions.md §10](./.github/copilot-instructions.md).

---

## Infra map (`infra/`)

[`infra/main.bicep`](./infra/main.bicep) wires nine modules in this order:
`network → monitoring → identity → acr → storage → storageState → keyvault →
containerEnv → controlApp`. Each one is a single small file under
[`infra/modules/`](./infra/modules/). Legacy Function-App-era Bicep is
preserved under [`legacy/infra/`](./legacy/infra/) (do **not** add a new
module that imports from there).

Public ingress lands on the `api` sidecar at `:8080`. All other sidecars
listen on loopback only:
- frontend nginx → `127.0.0.1:8081`
- terminal ttyd → `127.0.0.1:7681`
- redis → `127.0.0.1:6379`

---

## Common agent mistakes (tripwires)

These have actually happened in past sessions. The remediation is documented
where the failure was found, but knowing them up front saves a lot of time.

1. **Do not import `azure.functions`.** It is not in `pyproject.toml`. Any
   such import will load fine in dev (because the package is installed
   system-wide) and crash in the Container Apps image.
2. **Do not call `from services.X` or `from auth.X`** (no `api.` prefix).
   Those bare imports were the legacy Function-App style and any new module
   doing that will break in tests because there is no sys.path bridge anymore.
3. **Never issue a SAS token to the browser** and never re-introduce
   `generate_blob_sas`, `get_user_delegation_key`, or
   `BlobSasPermissions` in [api/services/storage_data.py](./api/services/storage_data.py).
   See the load-bearing comment at the bottom of that file.
4. **Never bind `ttyd` to anything other than `127.0.0.1`.** The api sidecar
   is the only legitimate client; the public ingress targets `:8080` only.
5. **Do not write a `requirements.txt`.** Edit `[project].dependencies` in
   [pyproject.toml](./pyproject.toml) then `uv lock --upgrade-package <name>`
   and commit `pyproject.toml` + `uv.lock` together.
6. **Do not edit anything under `legacy/`.** It is reference only — no
   bug-fix backports, no imports, no Bicep references, no test runs.
7. **Order in `api/main.py`:** any new `/api/*` router goes **above** the
   `frontend_proxy.router` include — otherwise the catch-all swallows your
   route and serves index.html.
8. **Storage `publicNetworkAccess` is `Disabled` in production.** Do not add
   a production code path, dashboard button, or environment toggle that
   flips it on. The only sanctioned exception is the manual local-debug
   helper [scripts/dev/storage-public-access.sh](./scripts/dev/storage-public-access.sh)
   (`on` opens an IP-allowlisted window for the caller, `off` restores).
   Do not bypass the script with `--default-action Allow`, `bypass:
   AzureServices`, or a wider IP range — see
   [.github/copilot-instructions.md §9](./.github/copilot-instructions.md).
9. **Never reach for Azure Run Command.** `ManagedClusters.begin_run_command`
   and `VirtualMachines.begin_run_command` were both removed (~30 s slow,
   ARM-rate-limited). Use [api/services/monitoring.py](./api/services/monitoring.py)
   `k8s_*` direct-API helpers for Kubernetes; for shell-only work see the
   contract in [api/services/terminal_exec.py](./api/services/terminal_exec.py).
   The api / worker images intentionally do **not** ship `kubectl` / `azcopy` /
   `elastic-blast` — those live in the `terminal` sidecar.

---

## Validation cheatsheet

| Layer | Command |
|-------|---------|
| Backend tests | `uv run pytest -q api/tests` |
| Backend smoke | `AUTH_DEV_BYPASS=true uv run uvicorn api.main:app --reload --port 8080` then `curl :8080/api/health` |
| Backend lint | `uv run ruff check api` |
| Worker | `uv run celery -A api.celery_app worker -l info` (needs local Redis) |
| Frontend build | `cd web && npm run build` |
| Infra preview | `azd provision --preview` |
| Local 2-sidecar Compose | `docker compose -f scripts/dev/docker-compose.local.yml up --build` |

---

## Repository conventions (most-violated, repeated here)

- **English only** in source / commits / docs / UI strings. (Korean only in
  the conversation with the user.)
- **Conventional Commits** (`feat:`, `fix:`, `chore:`, `docs:`, …).
- **Per-feature change notes** in `docs/features_change/YYYY-MM/YYYY-MM-DD-<name>.md`
  before each behaviour-changing commit.
- **No new dependency without justification** in the PR description.
- **Tests live next to their code** (`api/tests/`); cross-cutting only at root `tests/`.

---

## When this map is wrong

If you change directory layout, route prefixes, or the legacy/active split,
update this file *in the same change*. The map is the first thing future
agents read; an out-of-date map costs more than the change itself.
