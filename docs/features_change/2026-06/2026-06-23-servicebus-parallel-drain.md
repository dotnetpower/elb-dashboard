---
title: Service Bus drain — optional parallel submit fan-out
description: Add a default-OFF SERVICEBUS_DRAIN_CONCURRENCY knob that runs the per-message sibling submit concurrently within one drain tick, so a burst of queued BLAST requests clears in one tick instead of serialising N submit latencies.
tags:
  - blast
---

# Service Bus drain — optional parallel submit fan-out

## Motivation

The Service Bus request-queue consumer (`api.tasks.servicebus.drain_and_resubmit`)
bridged each message to the sibling `/v1/jobs` execution plane **serially**: one
drain tick processed up to `SERVICEBUS_DRAIN_MAX_MESSAGES` (50) messages, each
blocking on the synchronous sibling submit. With a submit latency of ~600 ms a
full tick took ~30 s — far longer than the 10 s beat interval — so a parallel
burst of submissions accumulated a growing backlog. Throughput was effectively
capped at `1 / submit_latency` (~1.6 msg/s) regardless of how many requests
arrived at once.

## User-facing change

None by default. This adds an operator throughput knob that is **off unless
explicitly enabled** (charter §12a Rule 4). Existing deployments behave exactly
as before until `SERVICEBUS_DRAIN_CONCURRENCY` is raised.

## API / IaC diff summary

- `api/services/service_bus.py` — `drain_requests` gains a keyword-only
  `max_concurrency: int = 1`. When > 1 the per-message handler bodies (the slow
  sibling submit) run on a bounded `ThreadPoolExecutor`; message **settlement
  stays on the main thread in receiver order**, because an Azure Service Bus
  receiver and its message locks are not safe to touch from multiple threads.
  The per-tick redelivery guard (`seen`) is still evaluated on the main thread
  before any handler runs, so parallelism never changes *which* messages are
  processed — only how fast their submits complete. Default `max_concurrency=1`
  is byte-for-byte the legacy serial loop (no thread pool is created).
- `api/tasks/servicebus/tasks.py` — new `SERVICEBUS_DRAIN_CONCURRENCY` env knob
  (clamped to `[1, 32]`, fail-safe to 1 on a non-numeric value so a bad override
  can never crash module import / the worker). The drain task forwards it and now
  logs one structured line per non-empty tick
  (`servicebus drain tick received=… completed=… concurrency=…`) and returns
  `concurrency` in its result dict for observability.

## Safety notes

- **Gate stays OFF until issue #2 (correlation-id atomic claim) lands.** Parallel
  submit widens the window in which two duplicate deliveries of the same
  `external_correlation_id` could be submitted concurrently (the bridge
  `get → upsert` is a non-atomic read-modify-write). Enabling the fan-out before
  the atomic claim would increase the duplicate-BLAST-run risk, so the default
  remains serial.
- Settlement, bounds (`max_messages`), and the redelivery guard are unchanged.
- No Storage `publicNetworkAccess` change, no SAS to the browser, no new Azure
  resource — purely an in-worker concurrency change.

## Validation

- `uv run pytest -q api/tests/test_service_bus_drain_loop.py api/tests/test_servicebus_tasks.py`
  — 51 passing, including three new parallel-path tests:
  `test_parallel_drain_maps_each_action_and_settles_once` (action/settlement
  mapping preserved), `test_parallel_drain_isolates_one_handler_exception`
  (one raising handler abandons only its own message),
  `test_parallel_drain_actually_runs_handlers_concurrently` (a 4-way
  `threading.Barrier` proves the handlers run at the same time), plus
  `test_serial_default_creates_no_thread_pool` (the default path spawns no pool)
  and `test_parallel_drain_handles_multiple_batches` (70 messages across 3
  receive batches).
- `uv run ruff check api` — clean.
