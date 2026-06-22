---
title: Service Bus drain — single-flight lease against overlapping ticks
description: Add a default-OFF SERVICEBUS_DRAIN_SINGLEFLIGHT gate that takes a short-lived, queue-scoped Redis lease before draining so two overlapping beat ticks or two workers cannot race the same request queue. Fail-open on a Redis error.
tags:
  - blast
---

# Service Bus drain — single-flight lease

## Motivation

The beat scheduler enqueues `drain_and_resubmit` every 10 s. If a tick's drain
runs longer than that interval (a burst with slow sibling submits), the next
tick is enqueued while the previous one is still running, and with worker
concurrency 4 several drain tasks can execute at once — all competing for the
same Service Bus request-queue receiver. The atomic claim (#2) already prevents
duplicate *submits*, but the overlapping ticks still waste receiver contention,
burn message lock churn, and flood the logs. A single-flight lease removes that
wasted racing.

## User-facing change

None by default. Gated behind `SERVICEBUS_DRAIN_SINGLEFLIGHT` (default-OFF,
charter §12a Rule 4). When off, every tick drains exactly as before.

## API / IaC diff summary

- `api/tasks/servicebus/tasks.py`
  - `_acquire_drain_lock(queue_name)` / `_release_drain_lock(token, queue_name)` —
    a **queue-scoped** Redis lease via `get_broker_redis_client`
    (`SET key token NX EX ttl`). The release is an atomic compare-and-delete Lua
    script so a tick only frees a lease it still owns (never one a later tick
    re-acquired after this one's TTL expired).
  - `drain_and_resubmit` takes the lease first; a lost lease returns
    `{"skipped": "locked"}` (logged at DEBUG) instead of racing, and the lease is
    released in a `finally`.
  - `_DRAIN_LOCK_TTL` (`SERVICEBUS_DRAIN_LOCK_TTL_SECONDS`, default 120 s, floored
    at 10 s, fail-safe on a bad value) is the backstop that frees a lease whose
    holder crashed before the `finally` ran.
  - **FAIL-OPEN**: any Redis error during acquire/release degrades to the legacy
    every-tick drain (the lease is an optimisation, not a correctness gate), so a
    broker blip never stalls the drain.

## Safety notes

- Enable alongside `SERVICEBUS_DRAIN_CONCURRENCY>1` + `SERVICEBUS_ATOMIC_CLAIM`:
  the lease cuts overlapping-tick contention, the claim guarantees single-submit.
- `_DRAIN_LOCK_TTL` must exceed a normal tick's drain time; a too-small TTL lets
  a slow drain's lease expire and a second tick start (the atomic claim still
  prevents duplicate submits, so the worst case is the contention this feature
  removes, never a duplicate run).
- The `resident` consumer does not take this lease (it is a single in-process
  loop guarded by its own lock); the atomic claim covers resident + beat overlap.

## Validation

- `uv run pytest -q api/tests/test_servicebus_tasks.py` — 9 new lease tests:
  off-never-touches-Redis, acquire-then-release (with queue-scoped key parity),
  contended-tick-skips (no drain, no foreign release), Redis-error-fails-open,
  queue-scoped key, and TTL env fail-safe + floor.
- `uv run pytest -q api/tests/test_persona_matrix.py` — green (§12a Rule 2).
- `uv run ruff check api` — clean. 126 passing across the changed-area suites.
