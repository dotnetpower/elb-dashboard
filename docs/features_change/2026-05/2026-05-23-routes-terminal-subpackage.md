# 2026-05-23 — routes/terminal subpackage

## Motivation
Phase D (partial) of the SRP cleanup. The browser-terminal routes (WebSocket
proxy + legacy-VM 410-Gone endpoints) are clearly one domain — fold them under
`api/routes/terminal/`. The remaining `routes/*.py` flat files were left in
place because each is already a single-URL-prefix module with no shared prefix
that would justify grouping.

## Diff
- `api/routes/terminal_ws.py` → `api/routes/terminal/ws.py`
- `api/routes/terminal_legacy.py` → `api/routes/terminal/legacy.py`
- New `api/routes/terminal/__init__.py`.
- Compatibility shims at the legacy flat paths re-export `router`.
- `test_terminal_ws_origin.py` updated to import the real module path so its
  `_TERMINAL_WS_ALLOW_ANY_ORIGIN` attribute patch lands on the real module.

## Rationale for not grouping the remaining flat routes
Each of `acr.py`, `arm.py`, `audit.py`, `client_log.py`, `elastic_blast.py`,
`health.py`, `me.py`, `operations.py`, `resources.py`, `tasks.py`, `upgrade.py`,
`warmup.py`, `frontend_proxy.py`, `_blast_shared.py` already owns a single URL
prefix and has no sibling that shares a prefix. Forcing them into `ops/` /
`lifecycle/` umbrellas would introduce indirection (shims + main.py rewires)
without making the surface easier to navigate.

## Validation
- `uv run pytest -q api/tests` → 1260 passed in 61.02s
- `uv run ruff check api` → All checks passed
