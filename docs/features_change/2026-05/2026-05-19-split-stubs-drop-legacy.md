# Drop legacy/ tree and split stubs.py into focused route modules

**Date**: 2026-05-19
**Scope**: repo housekeeping — no user-visible behaviour change

## Motivation

Two long-standing cleanups landed together:

1. **`legacy/` was dead weight.** The Azure Functions tree, the old Function-App
   + Static Web App Bicep, and the Remote-Terminal-VM cloud-init have been
   "reference only — do not edit" for months. They are preserved in `git log`
   and have stopped being a useful local reference (the active code paths in
   `api/`, `infra/`, and `terminal/` long since diverged). Keeping the tree on
   disk also drew agents into hallucinating imports from it.
2. **`api/routes/stubs.py` had ballooned to ~3,968 lines** with 47 route
   handlers across `/api/aks/*`, `/api/acr/*`, `/api/blast/*`, `/api/warmup/*`,
   and `/api/audit/*`. It violated the "one concern per module" rule that
   `api/services/` already follows and made route-level changes painful (any
   blast results tweak required scrolling past every database / shard / oracle
   endpoint).

## User-facing change

None. Every route URL, request shape, response shape, status code, and
side effect is preserved exactly. The split is structural.

## Code change summary

### Route module split

| New module | Replaces section of `stubs.py` | Lines |
|------------|--------------------------------|-------|
| [api/routes/_blast_shared.py](../../../api/routes/_blast_shared.py) | helpers + module-level constants (was lines 1–932) | ~880 |
| [api/routes/aks.py](../../../api/routes/aks.py) | `aks_router` + handlers (was 942–1354) | ~430 |
| [api/routes/acr.py](../../../api/routes/acr.py) | `acr_build_router` (was 1355–1385) | ~40 |
| [api/routes/blast.py](../../../api/routes/blast.py) | `blast_router` + handlers (was 1386–3695) | ~2,360 |
| [api/routes/warmup.py](../../../api/routes/warmup.py) | `warmup_router` (was 3696–3924) | ~250 |
| [api/routes/audit.py](../../../api/routes/audit.py) | `audit_router` (was 3925–EOF) | ~55 |

The empty `resources_router` placeholder is gone — the real router is in
[api/routes/resources.py](../../../api/routes/resources.py) and was already
included after the placeholder in `main.py`.

`api/routes/stubs.py` is deleted.

### main.py rewiring

The single `from api.routes import stubs` import becomes explicit imports of
the five new route modules. The five `app.include_router(stubs.X)` lines
become `app.include_router(aks.aks_router)` / etc., in the same order, still
**above** the `frontend_proxy.router` include (charter §15, AGENTS.md
tripwire #7).

### Test updates

| Test | Change |
|------|--------|
| [api/tests/test_local_to_blast_job.py](../../../api/tests/test_local_to_blast_job.py) | `from api.routes._blast_shared import …` |
| [api/tests/test_blast_submit_route_options.py](../../../api/tests/test_blast_submit_route_options.py) | `from api.routes._blast_shared import …` |
| [api/tests/test_smoke.py](../../../api/tests/test_smoke.py) | `_stub_log` import + `_config_preview_from_payload` monkey-patch target |
| [api/tests/test_external_blast_api.py](../../../api/tests/test_external_blast_api.py) | monkey-patches now target `api.routes.blast` (call site) and read constants from `api.routes._blast_shared` |

Tests that monkey-patch helpers do so at the **call site** module (`blast`)
rather than the definition module (`_blast_shared`), because Python resolves
the helper names against `blast`'s module globals at call time. This is the
standard pytest convention.

### Legacy tree removal

`rm -rf legacy/` — 576 KiB, 47 files. Active doc references updated:

- [README.md](../../../README.md) — directory tree + "Legacy walkthrough" section dropped.
- [AGENTS.md](../../../AGENTS.md) — tripwire #6 replaced with "the `legacy/` directory no longer exists".
- [.github/copilot-instructions.md](../../../.github/copilot-instructions.md) — three references stripped (IaC table, §11 bullet, §13 bullet).
- [azure.yaml](../../../azure.yaml) — drop the "legacy Function App + SWA topology" paragraph.
- [docs/container-apps-migration.md](../../../docs/container-apps-migration.md) — opening paragraph.
- [docs/copilot/repo-layout.md](../../../docs/copilot/repo-layout.md) — tree + Bicep section.
- [docs/copilot/browser-terminal.md](../../../docs/copilot/browser-terminal.md) — opening paragraph.
- [api/__init__.py](../../../api/__init__.py), [api/services/blast_results_parser.py](../../../api/services/blast_results_parser.py), [api/tasks/openapi.py](../../../api/tasks/openapi.py) — docstrings.

References inside `docs/features_change/**` are kept verbatim (historical
records, immutable by convention).

## Validation

- `uv run pytest -q api/tests` → **672 passed** (was 672 before the change).
- `uv run ruff check api` → **All checks passed!**
- `grep -r "from api.routes.stubs\|from api.routes import stubs" api/ scripts/`
  → no matches.
- `grep -r "legacy/" --exclude-dir=docs/features_change` → no matches.

## API / IaC diff summary

- API: zero route/contract changes. Only module paths changed.
- IaC: zero changes.
- SPA: zero changes (no frontend file touched).
