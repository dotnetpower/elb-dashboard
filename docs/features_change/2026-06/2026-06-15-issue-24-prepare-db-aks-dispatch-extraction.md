---
title: "Issue #24 — extract AKS-fanout dispatch out of prepare_db route"
description: "Move the ~470-line _try_dispatch_aks_mode body from the prepare-db route into a service module behind a domain-error boundary, satisfying issue #24 acceptance criterion #2."
tags:
  - contributor
  - blast
---

# Issue #24 — `prepare_db.py` AKS-fanout dispatch moved to a service

## Motivation

Issue #24 (split oversized files violating SRP) acceptance criterion #2 asks to
move the cloud / data-plane logic out of the `POST /api/storage/prepare-db`
route. The `_try_dispatch_aks_mode` helper was a ~470-line function inside the
route module that mixed HTTP validation, AKS cluster-health / node-readiness /
kubelet-RBAC pre-flight, NCBI key listing, metadata start-marker writes, the
per-(account, db) lock, and the Celery dispatch — most of it tightly coupled to
`HTTPException`. A previous pass (2026-06-06) deliberately deferred the full
extraction to "its own focused PR with a domain-error / result-object
boundary"; this change is that PR.

## User-facing change

None. This is a pure structural refactor. The HTTP contract is byte-identical:
same status codes, same `409` `detail` objects (`code: aks_unavailable` /
`kubelet_rbac_missing`), same success response dict, same `mode=auto`
server-side fall-through.

## API / IaC diff summary

- **New** `api/services/storage/prepare_db_aks_dispatch.py`:
  - `try_dispatch_aks_mode(...) -> dict | None` — the moved body. Returns the
    success response dict on dispatch, `None` to fall through to server-side.
  - `AksDispatchError(status_code, detail)` — domain error raised instead of
    `HTTPException`; the module never imports FastAPI.
  - Input-format validation that previously used `common._check` (which raises
    `HTTPException`) is inlined to raise `AksDispatchError(400, …)` with the
    same message format. The NCBI snapshot / key-listing / taxonomy helpers are
    resolved through the `common` module so existing test monkeypatch seams keep
    working.
- **`api/routes/storage/prepare_db.py`** (1,527 → 1,066 lines):
  - Removed the `_try_dispatch_aks_mode` body; the route now calls
    `try_dispatch_aks_mode(...)` and translates `AksDispatchError` into
    `HTTPException(exc.status_code, exc.detail)`.
  - Updated the context header (`_try_dispatch_aks_mode` dropped from the entry
    points; AKS dispatch boundary documented). Dropped the now-unused `time`
    import. The `prepare_db`, `prepare_db_cancel`, `prepare_db_delete` server-side
    paths and the re-exported `_poll_copy_completion` / `_update_metadata` /
    `_is_stale_prepare_marker` / `_prepare_db_lock` surface (used by the
    `prepare_db_via_aks` task, `recover-prepare-db.py`, and tests) are unchanged.
- **`api/tests/test_prepare_db_aks_route.py`**: `_baseline_patches` now also
  patches `api.routes.storage.common._resolve_latest_dir` (the moved code
  resolves the NCBI snapshot through `common`); the route-module patch is kept
  for the server-side path.

No new dependency. No route, response, or env-var contract change.

## Validation evidence

- `uv run ruff check api` → All checks passed.
- `uv run pytest -q api/tests` → **3669 passed, 3 skipped**.
- Focused: `test_prepare_db_aks_route.py`, `test_prepare_db_hardening.py`,
  `test_prepare_db_routes.py`, `test_prepare_db_delete_route.py`,
  `test_storage_shared_taxonomy.py`, `test_prepare_db_aks_task.py` →
  **56 passed**.

## Remaining issue #24 work (still open)

- SettingsPanel sections still over ~600 lines: `TelemetrySection` (626),
  `PublicHttpsSection` (663), `DiagnosticsSection` (745), `VnetPeeringSection`
  (749).
- Priority 2 frontend: `ClusterBento.tsx` (948) and `web/src/api/blast.ts` (811)
  not yet split.
