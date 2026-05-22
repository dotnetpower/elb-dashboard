# AGENTS.md — Navigation index for AI agents

> **Policy lives in [.github/copilot-instructions.md](./.github/copilot-instructions.md).**
> This file is the *map* — start here to find the right code fast, then read the
> charter for *how* to change it.

---

## TL;DR for a fresh session

1. **Active backend** is [`api/`](./api/) (FastAPI + Celery, Python 3.12, uv-managed).
   The retired Azure Functions tree has been removed from the repository
   (see [docs/container-apps-migration.md](./docs/container-apps-migration.md)
   for the target architecture).
2. **Active deploy target** is one Azure Container App `ca-elb-dashboard` with
   six sidecars. Bicep entry point: [`infra/main.bicep`](./infra/main.bicep).
3. **Local bring-up**:
   ```bash
   uv sync --all-groups          # creates .venv on Python 3.12
   uv run pytest -q api/tests    # ~980 passing
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
| Change SPA dashboard cards | [web/src/pages/Dashboard.tsx](./web/src/pages/Dashboard.tsx) + [web/src/api/endpoints.ts](./web/src/api/endpoints.ts) | Add the matching backend route under [api/routes/monitor/](./api/routes/monitor/) |
| Update browser terminal | [terminal/Dockerfile](./terminal/Dockerfile) + [terminal/entrypoint.sh](./terminal/entrypoint.sh) (ttyd loopback `127.0.0.1:7681`) | WebSocket proxy in [api/routes/terminal_ws.py](./api/routes/terminal_ws.py) |
| Change auth/JWT validation | [api/auth.py](./api/auth.py) | Tests in [api/tests/test_smoke.py](./api/tests/test_smoke.py) |
| Bump the release version / change the header stamp | [scripts/dev/bump-version.sh](./scripts/dev/bump-version.sh) (`--dry-run` first) | Full policy + pipeline in [docs/copilot/version-management.md](./docs/copilot/version-management.md) |

---

## Backend route map (what is real vs stub)

Routers are wired in [api/main.py](./api/main.py#L84-L99). Stub routers return
HTTP 503 / 410 by design until their Celery tasks land.

| Prefix | Module | State |
|--------|--------|-------|
| `/api/health` | [api/routes/health.py](./api/routes/health.py) | real |
| `/api/me` | [api/routes/me.py](./api/routes/me.py) | real (MSAL bearer required unless `AUTH_DEV_BYPASS=true`) |
| `/api/monitor/*` | [api/routes/monitor/](./api/routes/monitor/) | real (read-only, never 500 — uses `_graceful` to degrade to empty payloads) |
| `/api/arm/*` | [api/routes/arm.py](./api/routes/arm.py) | real (ARM proxy under shared MI) |
| `/api/resources/*` | [api/routes/resources.py](./api/routes/resources.py) | real (synchronous wizard provisioning) |
| `/api/storage/*` | [api/routes/storage/](./api/routes/storage/) | real (Storage prepare-db + local-debug firewall helper) |
| `/api/terminal/ws` | [api/routes/terminal_ws.py](./api/routes/terminal_ws.py) | real (WebSocket → loopback ttyd) |
| `/api/terminal/{vm}/...` | [api/routes/terminal_legacy.py](./api/routes/terminal_legacy.py) | **410 Gone** by design — the VM model is retired |
| `/api/aks/*` | [api/routes/aks/](./api/routes/aks/) | real / task-backed AKS actions |
| `/api/acr/*` | [api/routes/acr.py](./api/routes/acr.py) | real / task-backed ACR build actions |
| `/api/blast/*` | [api/routes/blast/](./api/routes/blast/) | real / task-backed BLAST package; result analytics live in [api/routes/blast/results.py](./api/routes/blast/results.py) |
| `/api/warmup/*` | [api/routes/warmup.py](./api/routes/warmup.py) | real / task-backed warmup planning + status |
| `/api/audit/*` | [api/routes/audit.py](./api/routes/audit.py) | real append-blob audit log |
| catch-all `/*` (non-`/api/*`) | [api/routes/frontend_proxy.py](./api/routes/frontend_proxy.py) | reverse proxies to frontend sidecar at `127.0.0.1:8081` |

> **Order matters.** [api/main.py](./api/main.py) registers `/api/*` routers
> *before* the frontend catch-all. Adding a new prefix? Insert it above the
> `frontend_proxy` line.

---

## Backend module map (`api/`) / Frontend (`web/src/`) / Infra (`infra/`)

Moved to [docs/copilot/repo-layout.md](./docs/copilot/repo-layout.md) to keep
this map lean. Read on demand when you need the directory-level detail.

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
6. **The `legacy/` directory no longer exists.** The Azure Functions tree
   was deleted on 2026-05-19. Do not try to re-create it, do not back-port
   bug fixes "from legacy", do not import or reference paths under
   `legacy/`. If you need historical context, read
   [docs/container-apps-migration.md](./docs/container-apps-migration.md)
   or `git log` for the deletion commit.
7. **Order in `api/main.py`:** any new `/api/*` router goes **above** the
   `frontend_proxy.router` include — otherwise the catch-all swallows your
   route and serves index.html.
8. **Storage `publicNetworkAccess` is `Disabled` in production.** Do not add
   a production code path, dashboard button, or deployed-environment toggle
   that flips it on. The sanctioned exceptions are the explicit local-debug
   helpers:
   * [scripts/dev/storage-public-access.sh](./scripts/dev/storage-public-access.sh)
     (and `scripts/dev/local-run.sh storage-on|storage-off|storage-status`)
     — opens / closes the network surface only.
   * [scripts/dev/local-debug-auth.sh](./scripts/dev/local-debug-auth.sh)
     (and `scripts/dev/local-run.sh auth-on|auth-off|auth-status`) — the
     **enable / disable session toggle** for real MSAL login locally. It
     composes `grant-local-rbac.sh` + `storage-public-access.sh` + flips
     `AUTH_DEV_BYPASS` + restarts api/web in one idempotent shot, and on
     `auth-off` closes the storage surface again. Use this instead of
     hand-running the three helpers when an agent or developer needs to
     debug as their real `az login` identity.

   Local backend auto-open must go through `api.services.storage_public_access`
   and keep the `CONTAINER_APP_NAME` guard. Do not bypass the script with
   `--default-action Allow`, `bypass: AzureServices`, or a wider IP range — see
   [.github/copilot-instructions.md §9](./.github/copilot-instructions.md).
9. **Never reach for Azure Run Command.** `ManagedClusters.begin_run_command`
   and `VirtualMachines.begin_run_command` were both removed (~30 s slow,
   ARM-rate-limited). Use [api/services/monitoring.py](./api/services/monitoring.py)
   `k8s_*` direct-API helpers for Kubernetes; for shell-only work see the
   contract in [api/services/terminal_exec.py](./api/services/terminal_exec.py).
   The api / worker images intentionally do **not** ship `kubectl` / `azcopy` /
   `elastic-blast` — those live in the `terminal` sidecar.
10. **Do not redeploy to validate ordinary code changes.** Backend / frontend
    edits are validated by `uv run pytest`, `uv run uvicorn …`, `npm run dev`,
    or the `fullstack: start` VS Code task (host mode — no image build). Only
    run `scripts/dev/quick-deploy.sh`, `scripts/dev/postprovision.sh`,
    `az acr build`, or `azd provision` when the change touches sidecar layout,
    Container App template, `terminal/Dockerfile*` / `exec_server.py`, or
    `infra/*.bicep`, AND the bug cannot be reproduced in Tier 1/2a. State the
    reason in the change note. Charter §13 calls this out explicitly.
11. **Do not hand-edit `version` in `web/package.json` or `pyproject.toml`.**
   Always go through [scripts/dev/bump-version.sh](./scripts/dev/bump-version.sh)
   so the release commit + tag stay consistent and SPA header / backend image
   carry the same release version. Policy in
   [.github/copilot-instructions.md §13](./.github/copilot-instructions.md);
   full workflow in [docs/copilot/version-management.md](./docs/copilot/version-management.md).
12. **Version bump workflow requires dry-run and approval.** For version
   questions, compare `web/package.json` and `pyproject.toml`; for bump
   requests, run `scripts/dev/bump-version.sh --dry-run`, recommend `--major`,
   default auto, or `--release` / `--minor`, then wait for maintainer approval
   before running the non-dry-run command. `--patch` is intentionally rejected
   because the patch segment is the build number.

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
| Local debug as real az identity | `scripts/dev/local-run.sh auth-on` (RBAC + storage open + bypass=false + restart) → debug → `scripts/dev/local-run.sh auth-off` |

---

## Repository conventions (most-violated, repeated here)

- **English only** in source / commits / docs / UI strings. (Korean only in
  the conversation with the user.)
- **Conventional Commits** (`feat:`, `fix:`, `chore:`, `docs:`, …).
- **Python context headers**: every new `*.py` file must start with a natural
   module summary docstring plus `Responsibility`, `Edit boundaries`,
   `Key entry points`, `Risky contracts`, and `Validation`. Do not use the
   literal `AI Context Header.` label, and keep the fields synchronized when
   the code changes.
- **SRP gate for Python edits**: if a new or changed module's context header
   cannot describe one responsibility cleanly, split it by layer before adding
   more code. Routes handle HTTP/auth/response shaping, services handle reusable
   domain/cloud/data-plane logic, tasks handle long-running side effects, and
   tests cover one behaviour family.
- **Per-feature change notes** in `docs/features_change/YYYY-MM/YYYY-MM-DD-<name>.md`
  before each behaviour-changing commit.
- **GitHub issue hygiene**: if a change implements a registered issue, comment with
   the completed work and validation; close it only after acceptance criteria are met,
   otherwise leave it open with the remaining gap.
- **No new dependency without justification** in the PR description.
- **Tests live next to their code** (`api/tests/`); cross-cutting only at root `tests/`.

---

## When this map is wrong

If you change directory layout, route prefixes, or the legacy/active split,
update this file *in the same change*. The map is the first thing future
agents read; an out-of-date map costs more than the change itself.
