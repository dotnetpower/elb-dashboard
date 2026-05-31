# BLAST capacity gate — Stage 1 unit tests

**Issue**: [#23](https://github.com/dotnetpower/elb-dashboard/issues/23)
(Stage 1 of 5 — explicit "Each phase is a separate PR" in the issue body)
**Date**: 2026-05-31
**Layer**: backend tests (pure module)

## Motivation

`api/services/blast/capacity_gate.py` landed as the Stage 1 pure module for
the parallel BLAST admission control work (commit `bbbdc35`). It was checked
in without a unit suite because Stage 1 was "ship the helper, do not wire it
into submit". Without tests the next stages cannot land safely — a refactor
that breaks the atomic Lua reserve script or the decision tree would not
surface until Stage 3 enables `BLAST_GATE_ENABLED=true` in production.

## What changed

Tests (`api/tests/`):

- New `api/tests/test_blast_capacity_gate.py` — 34 unit tests covering:
  - `slot_hash_key` derivation including the `_unknown` fallback.
  - `Reservation.to_json` / `Reservation.from_json` round-trip + bytes input
    + malformed payload rejection.
  - Env-var clamping for `BLAST_GATE_MAX_SLOTS_PER_CLUSTER`,
    `BLAST_GATE_CPU_WATERMARK_PCT`, `BLAST_GATE_SLOT_TTL_S`,
    `BLAST_GATE_DEFAULT_DEMAND_CPU_M` (minimum / maximum bounds + garbage
    fallback to defaults).
  - `evaluate_capacity_gate` decision tree — admit + all eight deny reasons:
    `aks_unreachable`, `pool_not_found`, `pods_pending`, `cpu_watermark`,
    `memory_watermark`, `slot_cap_reached`, `reserved_cpu_exhausted`,
    `reserved_memory_exhausted`. Also a regression for the
    "not-ready nodes contribute zero headroom" branch.
  - `reserve_slot` — admits under capacity, returns `None` once the slot
    cap is reached, is idempotent for the same `job_id`, atomic under
    contention (10 callers against `max_slots=3` → exactly 3 succeed),
    and returns `None` when Redis raises.
  - `release_slot` — drops the field, idempotent for unknown jobs, swallows
    Redis errors.
  - `list_active_reservations` — empty on cold start, decodes persisted
    payloads, skips malformed entries, empty list on Redis error.

The suite uses a stdlib in-memory `_FakeRedis` that implements just the
surface the gate touches (`hset`, `hdel`, `hvals`, `hexists`, `hlen`,
`expire`, plus an `eval` that re-implements the Lua reserve script in Python
so the atomic semantics under test match production exactly). The fake is
injected via `monkeypatch.setattr("api.services.redis_clients.get_broker_redis_client", ...)`
rather than the `sys.modules["redis"]` trick used by `test_redis_clients.py`
— this is the right boundary because the capacity gate imports
`get_broker_redis_client` directly and never touches the redis-py
constructor.

## Backward compatibility

- Test-only addition; no production code changes.
- The existing capacity gate module is unchanged. Stage 2 (history-based
  demand prediction) and Stage 3 (submit-task wiring behind
  `BLAST_GATE_ENABLED`) can land in follow-up PRs without re-litigating any
  contract this suite locks in.

## Validation

- `uv run pytest -q api/tests/test_blast_capacity_gate.py` — 34 passes,
  ~2.8s.

## Acceptance (issue #23 Stage 1 only)

- [x] Decision tree branches covered for admit + every deny reason.
- [x] Reserve / release primitives covered including the atomic contention
      guarantee that protects the `max_slots` invariant.
- [x] Env clamping covered including garbage / out-of-range values.
- [x] No wiring into submit task (deferred to Stage 3 per the issue body).

## Out of scope (issue #23 Stages 2-5, separate PRs)

- Stage 2: per-(program, database) demand history with Tier 2 per-program
  presets — `predict_demand` signature is already forward-compatible.
- Stage 3: workdir isolation, `submit_task` wiring behind `BLAST_GATE_ENABLED`,
  and the `waiting_for_capacity` requeue path.
- Stage 4: Bicep changes for new env defaults + a `_safe_delay` gate signal
  service.
- Stage 5: telemetry — Application Insights traces for every gate decision
  plus a dashboard tile rendering active reservations per cluster.

Each subsequent stage will land in its own PR with its own change note and
will need to keep this test suite green.
