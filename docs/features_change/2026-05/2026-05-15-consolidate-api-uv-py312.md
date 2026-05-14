# 2026-05-15 — Consolidate api/, adopt uv, bump Python 3.12, retire Functions

**Scope**: top-level layout, packaging, runtime.

## Motivation

The repo carried two parallel backends:

- `api/` — retired Azure Functions v2 + Durable Functions tree, no longer the
  deploy target after the move to Azure Container Apps.
- `api_app/` — the new FastAPI + Celery backend, in active use.

Two trees with overlapping module names (`services/`, `routes/`, …) cost
agent time on every search and made `from services.X` style imports break in
subtle ways. Bring them into one tree, name it `api/`, and at the same time
unify on `uv` + Python 3.12 so every developer / CI / Docker image runs on
the same exact toolchain.

## User-facing change

None — this is a structural cleanup. The deployed `/api/*` surface is
unchanged.

## API / IaC diff summary

### Layout

- `api_app/` → `api/` (`git mv`, history preserved).
- `api/` (legacy Functions tree) → `legacy/functionapp/`.
- `infra/legacy/` → `legacy/infra/`.
- `scripts/cloud-init/` → `legacy/cloud-init/`.
- `scripts/dev/{bootstrap-local,deploy-api,generate-client-secret,setup-keyvault,start-azurite,teardown-local}.sh`
  → `legacy/scripts/dev/`.

### Dead code removed

- `api/services/compute.py` — VM lifecycle helpers, 0 callers in active routes.
- `api/services/ssh_exec.py` — paramiko helper, only `compute.py` imported it.
- `api/_http_utils.py` — Functions-shaped helpers (`func.HttpResponse`); would
  crash at import in the Container Apps image because `azure-functions` is
  intentionally not in `pyproject.toml`.
- `infra/modules/platform.bicep` — orphan, byte-identical duplicate of
  `legacy/infra/platform.legacy.bicep`.
- `docs/ui-proposals/` (6 HTML prototypes, ~125 KB) — zero references in the
  active codebase.

### Packaging — uv + pyproject

- New `pyproject.toml` `[project]` with the full dependency set + a
  `[dependency-groups].dev` for pytest / pytest-asyncio / ruff / mypy.
- `uv.lock` (1297 lines) is now the source of truth.
- `.python-version` pins **3.12**.
- `api/requirements.txt` removed.
- `api/Dockerfile` rewritten as a 2-stage build using
  `ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim` to materialise the
  locked venv, then `python:3.12-slim` runtime — no `pip` or `uv` in the
  final image.
- `terminal/Dockerfile`: `ubuntu:22.04` + deadsnakes PPA → `ubuntu:24.04`
  with native `python3.12` (PPA dependency dropped).

### Tooling consistency

- `.vscode/launch.json` + `.vscode/tasks.json`: every command goes through
  `uv run …`; removed manual `VIRTUAL_ENV` exports.
- `.vscode/settings.json`: `python.defaultInterpreterPath` +
  `python.testing.pytestEnabled`.
- `scripts/dev/preflight-check.sh`: `uv` added to required-tool list.
- `scripts/dev/setup-app-registration.sh`: dropped the legacy
  `api/local.settings.json` writer + OBO `API_CLIENT_SECRET` next-steps;
  `web/.env.local` default `VITE_API_BASE_URL` → `http://localhost:8080`.
- `web/vite.config.ts`: dev proxy default → `http://localhost:8080`.
- `pytest.ini` `testpaths = api/tests`; `pyproject.toml` `src = ["api"]`,
  `target-version = "py312"`, `mypy.python_version = "3.12"`,
  `extend-exclude += "legacy"`.
- `.dockerignore`: drop `api/.python_packages` reference, prune `legacy/`.

### Docs

- `docs/container-apps-migration.md` retitled "Container Apps Architecture
  Reference"; trimmed migration narrative (Phase 0–5, Cutover, Rollback,
  Risks, Open Decisions, First Slice, Route Migration Map, Cost Comparison
  to prior plans, frontend SWA Old-vs-New table). 1327 → ~940 lines.
- `docs/auth.md`: every "Function App" / "system-assigned MI" → "api sidecar"
  / shared user-assigned MI `id-elb-control`; deleted Remote Terminal VM MI
  section + §4 "Virtual Machines" table; corrected Bicep modules table;
  rewrote §6 Security Notes (Storage actually Disabled, not Enabled).
- `README.md`: walkthrough section (295 lines) extracted to
  `legacy/walkthrough.md`; dashboard preview / Architecture Planning /
  Roadmap / Authentication sections retoned for the sidecar model. 537 →
  236 lines.

## Validation

- `uv sync --all-groups` creates `.venv` on Python 3.12.10.
- `uv run pytest -q api/tests` → **45 passed** (after this commit; further
  tests added in the next two commits bring the count to 56).
- 25-module import smoke clean (`api`, `api.main`, `api.celery_app`,
  `api.services.*`, `api.routes.*`).
- `uv run ruff check api` baseline: 125 (pre-existing style; no regressions).
- `uv run uvicorn api.main:app --host 127.0.0.1 --port 8080` →
  `/api/health` 200, `/api/docs` 200.
- `az bicep build infra/main.bicep` exit 0 (5 pre-existing
  `no-hardcoded-env-urls` warnings; unrelated).

## Cross-repo consistency

`AGENTS.md` (added in commit 3) records the active-vs-retired layout for
future agents.
