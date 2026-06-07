---
title: Bound the submit-gate lock registry and size the asyncio executor for asyncio.to_thread
description: Caps the single-flight per-key lock registry so a long-lived process cannot leak it, and makes API_THREADPOOL_TOKENS also size the asyncio default executor that backs asyncio.to_thread (JWT validation, SSE log streams) — not just the AnyIO limiter.
tags:
  - operate
  - blast
---

# Submit-gate lock registry bound + asyncio executor sizing (2026-06-07)

Two stability hardenings found by a self-critique of this session's earlier
single-flight change plus a deeper concurrency audit.

## 1. Bound the single-flight lock registry (self-critique finding)

The single-flight fix added a `key → threading.Lock` registry
(`_INFLIGHT_LOCKS`) to collapse the 50-parallel submit-gate stampede. Unlike
`_cache`, which self-expires on its 5 s TTL, the lock registry was only cleared
by `reset_submit_gates_cache()` (tests only). In a long-lived api/worker process
that touches many distinct gate keys — `memfit:{sa}:{db}:{machine_type}` is the
widest — the registry could grow without bound (a slow memory leak).

Fix: cap the registry at `_MAX_INFLIGHT_LOCKS = 512`. When the ceiling is hit,
the registry is dropped wholesale; threads already inside `with lock:` keep
their own lock reference and run to completion, so no held lock is released and
the worst case is a few keys re-creating a lock and doing one extra probe (the
5 s `_cache` still backs correctness). 512 is far above the realistic
distinct-key count for one deployment, so the drop is rare.

## 2. Size the asyncio default executor, not just AnyIO

`api/app/lifespan.py::_configure_threadpool_capacity` set the **AnyIO**
thread-limiter from `API_THREADPOOL_TOKENS`, and its comment claimed this backs
"every … `asyncio.to_thread` call". That is inaccurate: `asyncio.to_thread`
uses the **event loop's default executor**
(`ThreadPoolExecutor(max_workers=min(32, cpu+4))`), which AnyIO does not govern.
On a small-vCPU Container App that pool can be ~5 threads.

Consequence: a burst of concurrent SSE log streams (`api/routes/blast/logs.py`
spawns a blocking Redis `xread` reader plus per-pod K8s log-follow threads, all
via `asyncio.to_thread`) could pin that tiny pool and **starve JWT validation**
(`api/auth.py` runs `await asyncio.to_thread(_validate_token, token)` on the
same default executor), stalling every authenticated request — exactly the kind
of instability the "50 parallel jobs" scenario surfaces.

Fix: when `API_THREADPOOL_TOKENS` is set, also replace the loop's default
executor with a `ThreadPoolExecutor(max_workers=tokens)` so both pools widen
together. Done at startup before any work is submitted to the default executor;
opt-in only (unset env preserves both library defaults). The misleading comment
is corrected to document the two distinct pools.

## API / IaC diff summary

- `api/services/blast/submit_gates.py` — `_key_lock` now caps `_INFLIGHT_LOCKS`
  at `_MAX_INFLIGHT_LOCKS` (512), dropping the registry wholesale on overflow.
- `api/app/lifespan.py` — `_configure_threadpool_capacity` now also calls
  `loop.set_default_executor(ThreadPoolExecutor(max_workers=tokens))`; docstring
  + call-site comment corrected to describe both pools.
- No IaC change. No auth/RBAC change (the executor swap is capacity-only;
  `_validate_token` still runs identically, just with adequate threads).

## Validation evidence

- `uv run pytest -q api/tests/test_blast_submit_gates.py` — 37 passed, including
  `test_inflight_lock_registry_is_bounded` (fills to the cap, one more key trips
  the wholesale drop) and the existing single-flight stampede test.
- `uv run pytest -q api/tests/test_lifespan_threadpool.py` — 3 passed
  (`test_configures_both_pools` asserts the asyncio executor `_max_workers`
  equals the tokens; `test_noop_when_unset` / `_when_invalid` assert the
  executor is NOT swapped without a valid env value).
- `uv run pytest -q api/tests` — 3095 passed, 3 skipped (no regression).
- `uv run ruff check` on both files — clean.
