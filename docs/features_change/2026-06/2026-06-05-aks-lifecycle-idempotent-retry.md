---
title: AKS start/stop tasks are now idempotent under retry and duplicate dispatch
description: start_aks / stop_aks used autoretry_for=(Exception,) over a non-idempotent AKS power LRO, so a transient poll blip, a duplicate Start/Stop click, or a manual Stop racing the idle auto-stop turned an effective success into a hard task ERROR plus wasted retries. They now treat ARM "already in/transitioning to the target power state" as an idempotent no-op success.
tags:
  - infra
  - operate
---

## Motivation

Deep-analysing the AKS lifecycle path surfaced a retry-semantics bug in the same
class as the prepare-db backoff fix: a retry policy that does not match the
operation's idempotency.

`start_aks` and `stop_aks` are decorated with
`autoretry_for=(Exception,) max_retries=3` and call `begin_start()` /
`begin_stop()` followed by `poller.result()` **unconditionally**, with no
power-state guard. But the AKS power LRO is **not idempotent**: ARM rejects
`begin_start` on a Running/Starting cluster (and `begin_stop` on a
Stopped/Stopping one) with `OperationNotAllowed` / `BadRequest` ("… is not in a
stopped state").

So any of the following turned an *effective success* into a hard task ERROR
plus up to 3 wasted ARM retries:

* a transient blip during the multi-minute `poller.result()` poll (token
  refresh, ARM 429/5xx on a poll, network hiccup) **after** ARM already accepted
  the operation — autoretry re-issues `begin_start`/`begin_stop` on a
  now-transitioning cluster;
* a duplicate Start/Stop click (the routes have no de-dup);
* a manual Stop racing the idle auto-stop (the evaluator guards
  `provisioning_state`, but the manual path did not).

The codebase already *knew* about this hazard — the auto-stop evaluator guards
`OperationNotAllowed`, and `auto_stop_aks` deliberately set `max_retries=0` to
avoid "up to 9 ARM stop attempts on a transitioning cluster" — yet the inner
`start_aks`/`stop_aks` still autoretried blindly.

## User-facing change

Start/Stop now converge to success when the cluster is already in (or
transitioning to) the requested power state, instead of surfacing a failed
lifecycle task in the dashboard audit while the cluster actually started/stopped
fine. A no-op start still runs the follow-on Auto-warm reconcile + OpenAPI
deploy the user asked for (both idempotent).

## API / IaC diff summary

`api/tasks/azure/lifecycle.py`:

* New `_is_already_in_target_power_state(exc)` — recognises the ARM
  `OperationNotAllowed` / "not in a stopped|running state" / "is already
  running|stopped" rejection on an `HttpResponseError`.
* `start_aks`: wraps `poller.result()`; on the marker error logs an INFO and
  falls through to the follow-on enqueues as a no-op (skips lifecycle-timing so
  a ~0 s duration never poisons the "last observed start took …" estimate).
  Returns `noop: bool` (additive).
* `stop_aks`: same treatment; returns `noop: bool` (additive).
* Genuinely transient errors (not the marker) still raise, so Celery's
  `autoretry_for` keeps retrying real failures.
* `delete_aks` left unchanged — `begin_delete` is idempotent in ARM (delete on a
  missing cluster is a 204 no-op), so its autoretry is already safe.

### Known tradeoff (documented, accepted)

A manual Stop issued *during* an in-flight Start also raises
`OperationNotAllowed` and is now reported as a no-op success rather than a hard
error. The cluster keeps running either way (ARM refused the stop, nothing
changed), and the next idle-auto-stop tick or a manual retry converges once the
start settles — strictly better than the old "3 retries then ERROR". The
auto-stop path already guards this case via `provisioning_state`.

## Validation evidence

* `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_auto_stop_task.py
  api/tests/test_aks_autostop_route.py` → 63 passed, including four new tests:
  `test_start_aks_treats_already_running_as_noop`,
  `test_start_aks_reraises_transient_error`,
  `test_stop_aks_treats_already_stopped_as_noop`,
  `test_stop_aks_reraises_transient_error`.
* Full suite `uv run pytest -q api/tests` → 2890 passed, 3 skipped.
* `uv run ruff check` (changed files) → clean.

## Deployment note

Baked into the `worker` image (Celery tasks) — takes effect for newly dispatched
Start/Stop after an `api` + `worker` image rebuild.
