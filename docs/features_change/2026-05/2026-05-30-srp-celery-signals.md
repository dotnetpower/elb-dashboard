# 2026-05-30 — SRP cleanup: extract Celery signal handlers into `api/celery_signals.py`

## Motivation

[api/celery_app.py](../../../api/celery_app.py) had a `Responsibility:`
line that read

> Celery app configuration, sidecar startup hooks, and terminal
> task-failure visibility for worker and beat sidecars.

— three concerns chained together. Reading the 386-line module
confirmed three distinct slices of code, each with its own dependency
fan-out:

1. **App configuration** (`Celery(...)`, `set_default()` / `set_current()`,
   queue routing, beat schedule) — touches the Celery API surface.
2. **Sidecar startup hooks** (`@worker_init`, `@worker_process_init`,
   `@beat_init` → cgroup reporter + telemetry init) — touches
   `api.services.cgroup_reporter` and `api.app.telemetry`.
3. **Terminal-state visibility** (`@task_failure`, `@task_internal_error`,
   `@task_revoked`, `@before_task_publish` and the
   `_record_task_terminal_state` helper) — touches
   `api.services.state_repo` and `api.services.event_emitter`.

Per charter §11 SRP gate:

> Routes own HTTP/auth/response shaping; services own reusable
> domain/Azure/Kubernetes/Storage logic; tasks own long-running side
> effects and progress checkpoints; tests own one behaviour family.

The single module mixed (1) Celery wiring with (2) cross-cutting telemetry
and (3) JobState write-back. Splitting along Celery-signal vs Celery-app
keeps each file inside one concern.

## User-facing change

**None.** Pure refactor. Signal handlers fire on the exact same Celery
signals, with byte-identical payloads. `celery_app` is still importable
as `from api.celery_app import celery_app`. The test surface
(`api.celery_app._on_task_failure`, `_on_task_revoked`,
`_on_worker_process_init`, etc.) is preserved via explicit module-level
re-exports.

## API / IaC diff summary

* **New file** [api/celery_signals.py](../../../api/celery_signals.py)
  (264 lines): receives the helpers
  (`_start_reporter`, `_now_iso`, `_task_job_id`, `_task_name`,
  `_record_task_terminal_state`) and the signal-handler functions
  (`_on_worker_init`, `_on_worker_process_init`, `_on_beat_init`,
  `_on_task_failure`, `_on_task_internal_error`, `_on_task_revoked`,
  `_on_before_task_publish`) with their `@*.connect` decorators. Module
  docstring states the import-time side-effect contract explicitly so a
  future maintainer does not lazy-load it from a worker task.
* [api/celery_app.py](../../../api/celery_app.py): 386 → 192 lines.
  * **Removed**: every signal handler + helper definition listed above
    and the now-unused `datetime` / `typing.Any` / `celery.signals`
    imports.
  * **Kept**: `Celery(...)` instantiation, `set_default()` /
    `set_current()`, `conf.update(...)` (time limits + queue routing +
    beat schedule), `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`
    constants, and the azure-SDK log silencer block.
  * **Added**: a single `from api import celery_signals as _signals`
    import after `celery_app` is defined (registers the handlers at
    import time) plus explicit `_on_task_failure = _signals._on_task_failure`-style
    re-exports for back-compat. The legacy attribute path used by
    `test_celery_failure_visibility.py` and `test_telemetry_init.py`
    keeps working.
  * Updated module docstring: `Responsibility:` is now a single sentence
    ("Instantiate the single `celery_app` …"); `Edit boundaries:`
    explicitly steers new signal handlers and JobState wiring into
    `api.celery_signals`; `Risky contracts:` documents the bottom-of-module
    import order requirement so a future agent does not move it.
* **No** route, task, test, Bicep, frontend, persona-matrix, or env-var
  change.

## Validation evidence

* Focused: `uv run pytest -q api/tests/test_celery_failure_visibility.py
  api/tests/test_telemetry_init.py` → **8 passed in 3.44s**.
* Wide: `uv run pytest -q api/tests` → **2152 passed, 3 skipped in
  35.58s**. Skips are the pre-existing
  `test_web_blast_parity_xml.py` skips (require
  `ELB_PARITY_CANDIDATE_DIR`).
* Lint: `uv run ruff check api/celery_app.py api/celery_signals.py` →
  **All checks passed!** after sorting `__all__` (RUF022).
* Consumer search: all 17 production sites still use
  `from api.celery_app import celery_app` (or `CELERY_BROKER_URL`); none
  needed an update. The 3 test sites that touch handler functions
  (`celery_app._on_task_failure`, `celery_app._on_task_revoked`,
  `celery_app._on_worker_process_init`) keep working through the
  re-exports.
* Frontend: no `web/src/**` files touched — `npm run build` not required.
* IaC: no Bicep touched — `azd provision --preview` not required.
* Diff audit: `git status --short` → `M api/celery_app.py` +
  `?? api/celery_signals.py` only; `git diff --stat` → 1 file modified
  (+48 / -242).

## Hardening discipline (§12a):

- [x] In scope: SRP refactor (Celery signal handlers split into their own
  module). No auth, RBAC, network ACL, JWT, ticket, CORS, or sanitisation
  surface changed.
- [x] RBAC change is single-PR safe (no role narrowed) — N/A, no RBAC
  change.
- [x] Persona Matrix tests pass for owner / contributor / reader /
  dev_bypass — wide-sweep green; handlers fire on the same signals with
  byte-identical payloads.
- [x] Reader allowlist unchanged — no Reader-required route touched.
- [x] Capability Probe passes locally — no new Azure surface, probe
  unaffected.
- [x] New guard ships default-OFF behind `STRICT_*` / `ENFORCE_*` env var
  — N/A, this PR moves code, it does not add a guard.
- [x] No `Depends(require_caller)` added to an SSE event stream — no SSE
  changes.
- [x] Change note (this file) summarises persona impact: every persona is
  byte-for-byte unaffected. The signal handlers still register at
  `api.celery_app` import time (now via a chained import of
  `api.celery_signals`), and their behaviour and observable output
  (logs, JobState rows, event emitter rows) are identical.
