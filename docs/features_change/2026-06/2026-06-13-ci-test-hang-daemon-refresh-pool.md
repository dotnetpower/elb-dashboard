---
title: Fix CI test hang from background refresh pools blocking interpreter exit
description: Replace the monitor and storage-usage ThreadPoolExecutor refresh
  pools with a bounded daemon worker pool so an in-flight refresh blocked on a
  stuck network call can never hang interpreter / pytest-xdist worker shutdown.
tags:
  - operate
  - tests
---

# Fix CI test hang from background refresh pools blocking interpreter exit

## Motivation

The GitHub Actions **Tests** workflow began hanging to its job timeout
(`[gw0] node down: Not properly terminated` at ~98-100% complete), turning the
dashboard red on nearly every push. The suite ran clean locally (~14 s) and
could not be reproduced on a developer laptop, by disabling Redis, by blocking
outbound sockets, or by faking credentials.

Root cause: `api/services/monitor_cache.py` and
`api/services/storage/usage_cache.py` each kept a process-wide
`concurrent.futures.ThreadPoolExecutor` for **fire-and-forget** background cache
refreshes. ThreadPoolExecutor worker threads are non-daemon and are *joined* by
the interpreter-shutdown hook `concurrent.futures.thread._python_exit`. On the
Azure-hosted CI runner the instance metadata endpoint (IMDS, `169.254.169.254`)
is reachable, so a background refresh that resolves a credential and calls
ARM/Storage actually performs a network round-trip that can block/retry for a
long time. If a worker finished its assigned tests while such a refresh was
in-flight, the worker's interpreter exit blocked forever joining that stuck
thread. Locally IMDS is unreachable, so the call fails fast and the thread
finishes — which is why it never reproduced off the runner. It was flaky
because it depended on a refresh being in-flight at worker shutdown.

## User-facing change

None for the dashboard UI. Internally, background monitor/storage-usage cache
refreshes now run on a bounded **daemon** worker pool
(`api/services/background_refresh.py`). Daemon worker threads are skipped by
both `_python_exit` and the interpreter's final non-daemon-thread join, so
process / xdist-worker shutdown is always clean even with an in-flight,
network-blocked refresh. Under a sustained upstream outage, excess refreshes are
now **dropped** (the stale cache is still served and the next poll re-attempts)
instead of being queued unbounded.

## Code / IaC diff summary

* New `api/services/background_refresh.py` — `DaemonRefreshPool`: a fixed set of
  lazily-started daemon worker threads draining a bounded queue; `submit` is
  non-blocking and drops on overflow.
* `api/services/monitor_cache.py` / `api/services/storage/usage_cache.py` —
  replaced the module-level `ThreadPoolExecutor` (+ `atexit` shutdown) with
  `DaemonRefreshPool`. Public test seam `_start_refresh_thread` is unchanged.
* `api/tests/conftest.py` — disable the deployment-only cgroup reporter under
  tests (removes a per-xdist-worker daemon thread that otherwise spams Redis
  connection errors and adds memory/threads on every worker).

## Validation evidence

* Repro before fix: a script submitting a 600 s blocking job to the old pool and
  exiting hung until killed (exit 124). After the fix the same shape exits in
  `< 1 s` (exit 0).
* New `api/tests/test_background_refresh.py` (incl. a `subprocess` exit-guard
  test asserting a blocked job does not delay interpreter exit) — 4 passed.
* `uv run pytest -q api/tests` — 3503 passed, 3 skipped.
* `uv run ruff check api` — clean.
