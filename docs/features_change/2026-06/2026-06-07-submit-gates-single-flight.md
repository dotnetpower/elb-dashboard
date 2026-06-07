---
title: BLAST submit gates collapse the 50-parallel cache stampede with single-flight
description: The ARM/Storage/ACR-backed submit gates now use per-key single-flight so a burst of concurrent submits for the same target fires one probe instead of N, preventing Azure throttling from fail-closing otherwise-valid submits.
tags:
  - blast
  - operate
---

# Submit gates single-flight under a 50-parallel burst (2026-06-07)

## Motivation

`api/services/blast/submit_gates.py` evaluates several pre-flight gates that
hit Azure: `_gate_aks_cluster` (ARM `list_aks_clusters`), `_gate_blast_database`
and `_gate_node_memory_fit` (Storage metadata), and `_gate_acr_images` (ACR
manifest lookups). Each gate cached its result for 5 s with a plain
`miss → probe → set` pattern over an unlocked module dict.

Under the real-world scenario of **~50 BLAST/API submits arriving in parallel
for the same cluster/database** (the exact case the user asked us to harden),
all 50 callers miss the 5 s cache simultaneously and each fires its own
ARM/Storage/ACR probe — a classic **cache stampede / thundering herd**. 50
concurrent `list_aks_clusters` / ACR manifest calls trip Azure ARM throttling
(HTTP 429); a throttled gate then degrades to `unknown` (or, for a strict
caller, blocks), so a burst of legitimate submits can fail-close even though
the cluster and database are perfectly healthy. There is no data corruption
(Python dict ops are atomic under the GIL), but the stability impact under
load is real.

## User-facing change

A burst of concurrent submits for the same target now does a single shared
probe; the rest reuse its result. Submits under load are far less likely to hit
spurious `cluster_check_unavailable` / `acr_check_unavailable` /
`database_check_unavailable` gate verdicts caused by self-inflicted throttling.

## API / IaC diff summary

- `api/services/blast/submit_gates.py`:
  - Added a per-key single-flight helper `_cached_or_compute(key, compute)`
    backed by a small `key → threading.Lock` registry (`_INFLIGHT_LOCK` guards
    only the registry, never a network probe, so distinct targets never
    serialise against each other). Fast path is an unlocked cache hit;
    on a miss the first caller takes the per-key lock, double-checks the cache,
    and runs `compute` exactly once while the rest of the burst waits and then
    reads the fresh cached value.
  - Refactored `_gate_aks_cluster`, `_gate_blast_database`,
    `_gate_node_memory_fit`, and `_gate_acr_images` to compute through
    `_cached_or_compute` (behaviour-identical results; only the concurrency
    coordination changed).
  - `reset_submit_gates_cache()` now also clears the in-flight lock registry.
- No IaC change. No auth/RBAC change.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_submit_gates.py` — 36 passed, including
  the new `test_aks_cluster_gate_single_flight_under_parallel_burst`: 50 threads
  call `_gate_aks_cluster` for the same cluster while the probe is held open;
  the test asserts all 50 get `ok` and the underlying `list_aks_clusters` ran
  **exactly once**.
- `uv run pytest -q api/tests` — 3091 passed, 3 skipped (no regression).
- `uv run ruff check api/services/blast/submit_gates.py api/tests/test_blast_submit_gates.py` — clean.

## Lifecycle timing note (audited, no change needed)

The start↔stop transition races (double-click Start, stop-racing-start, manual
stop vs idle auto-stop) were audited and are already guarded:
`start_aks`/`stop_aks` treat ARM's "already in target power state" rejection as
an idempotent no-op (`_is_already_in_target_power_state`), and the idle
evaluator keeps (never stops) when `power_state != Running` or
`provisioning_state != Succeeded`, so a cluster mid-start (~5 min `Starting`)
is never stopped out from under the user even if the `last_started_at` stamp
is lost. No behaviour change was made there.
