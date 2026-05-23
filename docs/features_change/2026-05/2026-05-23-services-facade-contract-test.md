# 2026-05-23 — services facade contract regression test

## Motivation
Phase A left 37 compatibility shims at `api/services/<prefix>_<name>.py` paths.
Each shim either explicitly re-exports a fixed `__all__` list (for the smaller
modules) or uses a module-level `__getattr__` proxy (for the large ones like
`k8s.monitoring` with 30+ symbols).

Both patterns can silently rot:
- Explicit `__all__` shim: real module renames or drops a listed symbol →
  shim import raises ImportError on the next deploy.
- `__getattr__` proxy shim: someone replaces the proxy with a stale import
  list → new symbols added to the real module stop forwarding.

The repo already had a similar guard for `api/tasks/*` facades
(`test_tasks_facade_contract.py`). This commit adds the equivalent for
`api/services/*` shims.

## Diff
- New `api/tests/test_services_facade_contract.py` with 4 parametrized tests
  per shim:
  1. The shim and the real subpackage module both import cleanly.
  2. Every name in the shim's `__all__` resolves to the same object on both
     modules (catches drift in explicit shims).
  3. For `__getattr__`-proxy shims, attribute access forwards to the real
     module (catches a broken proxy).
  4. Every shim file on disk is registered in `_FLAT_SHIMS`, and every
     non-private submodule in `services/{blast,db,k8s,openapi,storage,warmup}/`
     has a corresponding shim entry (catches a new module that forgot a shim).

## Validation
- `uv run pytest -q api/tests/test_services_facade_contract.py` → 116 passed
- `uv run ruff check api/tests/test_services_facade_contract.py` → All checks passed
