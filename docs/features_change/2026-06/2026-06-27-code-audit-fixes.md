---
title: Code audit fixes — race, resource cleanup, narrowing asserts, input caps
description: Round of hardening fixes surfaced by a code-audit pass — module-level httpx client race in the frontend proxy, nested cleanup on the OpenAPI proxy stream, four narrowing asserts replaced with raise so they survive Python -O, and a length cap on every job_id path parameter.
tags:
  - operate
  - blast
---

# Code audit fixes — race, resource cleanup, narrowing asserts, input caps

## Motivation

A code-audit pass surfaced 28 candidate issues across the codebase. After
verifying each against the actual source, **12 turned out to be false positives**
(already guarded, e.g. `_CLAIMS_CACHE_SOFT_CAP`, `cas_retry`, existing body-size
guard, in-place timeouts on every `httpx` call site). This change ships the
**real** issues that fit a single, low-risk slice.

## Fixes shipped

### Critical

* **Module-level `httpx.AsyncClient` race in the frontend reverse proxy.** The
  lazy init in `api/routes/frontend_proxy.py::_get_client` had a check-then-set
  on the module global with no lock — two coroutines that raced the first
  request after startup could both construct a client; one was silently
  overwritten, leaking its connection pool past process exit because
  `close_client()` only closes the last assignment. Fix: a `threading.Lock()`
  guard around the lazy init.

### High

* **Cleanup leak on the OpenAPI proxy stream.** `_body_iter` closed both
  `upstream` and `client` inside one `finally` block — if `upstream.aclose()`
  raised, `client.aclose()` was skipped, leaking the whole client connection
  pool. Fix: nested try/finally so `client.aclose()` always runs.

### Medium

* **Four production `assert` statements promoted to `raise RuntimeError`.** All
  four are mypy-narrowing asserts — under Python `-O` they would be no-ops, so
  the implicit "this is unreachable" guard disappears. Switched to
  `if … is None: raise RuntimeError(...)` which mypy still narrows AND survives
  `-O`. Sites: `api/tasks/azure/peering_nsg.py`, `api/services/aks/ensure_running.py`,
  `api/services/blast/compatibility.py`, `api/services/preference_concurrency.py`.

* **Length cap on every `job_id` Path parameter.** Ten BLAST routes accepted
  `job_id: str = Path(...)` with no length cap, letting a caller supply a
  multi-KB string that would then flow into Storage queries. Now all `job_id`
  Path params are `min_length=1, max_length=128` (matching the cap retry/cancel
  already used). Files: `result_analytics.py`, `results.py`, `results_export.py`,
  `logs.py`, `jobs.py`, `jobs_detail.py`, `jobs_lifecycle.py`.

## False positives identified during the audit

For the record (so a future audit does not re-raise them):

* `httpx` timeout coverage on `external_blast.py`, `openapi_proxy.py`,
  `storage/common.py`, `peering.py`, `public_access.py`, `webhooks.py`,
  `pricing.py`, `frontend_proxy.py` — every call site already passes an explicit
  `timeout=` (verified by grep).
* `_CLAIMS_CACHE` unbounded growth — already capped at
  `_CLAIMS_CACHE_SOFT_CAP=1024` with TTL + lock.
* `auto_stop.py` ETag silent-swallow — `cas_retry` already retries on
  `ResourceModifiedError` with bounded attempts.
* FastAPI request body size — `body_size_guard` middleware already enforces
  10 MiB (configurable via `MAX_REQUEST_BODY_BYTES`).
* SSE `Depends(require_caller)` violation — no current violation; preventative
  guard would belong in a docstring rule, not in code.
* Webhook URL SSRF — already covered by the `validate_webhook_url` allowlist
  shipped in the prior webhook change.
* ncbi/_eutils silent retry — retries already log at WARNING and re-raise after
  exhaustion.

## Remaining audit findings (NOT addressed in this change)

* **State-machine bypass**: 4 sites in `reconcile_task.py` / `cancel_task.py`
  call `repo.update(status=…)` directly instead of going through
  `_update_state`, which would otherwise sweep orphan `_progress` steps and
  emit the `blast` customEvent. Each site needs per-context analysis — left as
  a follow-up finding.
* **`service_bus_tracking` two-step update** — second `update_entity` can race
  with another writer; needs a composite-row or transaction redesign.
* **Beat overlap singleflight lock** — multiple beat reconcilers can run
  concurrently when a tick takes longer than its schedule.

## Validation evidence

* `uv run ruff check api` — all checks passed.
* `uv run pytest -q api/tests` — 4685 passed, 3 skipped, 0 failed.
* No new test added — these are localised defensive fixes; existing routes /
  reconcilers / proxies are covered by their own test suites which all pass.
