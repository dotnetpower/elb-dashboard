---
title: BLAST capacity gate — Stage 3a (live signal resolver)
description: Add api.services.blast.capacity_signals to fetch live CPU/memory request pressure, top-node telemetry, and pending pod counts behind the shared cluster-health cache so the submit-time gate runs without DoSing the AKS API.
tags: [blast, architecture]
---

# BLAST capacity gate — Stage 3a (live signal resolver)

> Issue: [#23](https://github.com/dotnetpower/elb-dashboard/issues/23) — *AKS Capacity Gate for BLAST submit*.
> Builds on: [Stage 1 change note](2026-05-31-capacity-gate-stage1-tests.md).
> Followed by: [Stage 3 change note](2026-05-31-stage3-submit-wiring.md).

## Motivation

Stage 1 of the capacity gate ships the pure decision logic
(`api.services.blast.capacity_gate.evaluate_capacity_gate`) and the slot
reservation primitives. To wire those into the BLAST submit task we need a
single function that returns the three live signals the gate evaluator
expects — without making every BLAST submit pay full-cost K8s API + Container
Insights round-trips. That helper is `capacity_signals.resolve_capacity_signals`.

## User-facing change

None directly. The helper only becomes observable once Stage 3 flips
`BLAST_GATE_ENABLED=true`.

## API / IaC diff summary

### New module `api/services/blast/capacity_signals.py`

Public surface:

```python
@dataclass(frozen=True)
class CapacitySignals:
    pressure: dict[str, Any] | None   # k8s_node_request_pressure() shape
    top_nodes: list[dict[str, Any]] | None  # k8s_top_nodes() shape
    pending_pods: int                 # blastpool namespace, Pending phase

def resolve_capacity_signals(
    credential, subscription_id, resource_group, cluster_name,
    *, pool_name="blastpool",
) -> CapacitySignals

def signal_cache_ttl_s() -> int   # env BLAST_GATE_SIGNAL_CACHE_S, default 30, clamped 5–300
def signal_cache_stale_s() -> int # env BLAST_GATE_SIGNAL_STALE_S, default 120, clamped 10–600
```

Behaviour:

- All three K8s lookups are wrapped in private `_safe_*` helpers that
  **never raise** — every failure degrades to `pressure=None`,
  `top_nodes=None`, `pending_pods=0`. This matches the dashboard's existing
  "monitor routes never 500" contract.
- The composite call is memoised through
  `api.services.cluster_health.cached_snapshot_with_cluster_gate` keyed on
  `blast:capacity:signals:{subscription}:{resource_group}:{cluster}`. The
  TTL defaults to 30 s with a 120 s stale-tolerance window so a single
  cluster's BLAST submits do not stampede the K8s API even at high arrival
  rates.

### New tests `api/tests/test_blast_capacity_signals.py` (7 tests)

- `test_resolve_capacity_signals_happy_path`
- `test_resolve_capacity_signals_pressure_failure_degrades`
- `test_resolve_capacity_signals_counts_pending_pods`
- `test_safe_pending_pods_count_filters_phase` (Running / Pending / Failed mix)
- `test_safe_pending_pods_count_degrades_on_error`
- `test_signal_cache_ttl_defaults`
- `test_signal_cache_ttl_clamping`

## Validation evidence

```
$ uv run pytest -q api/tests/test_blast_capacity_signals.py
... 7 passed in ~1s ...
$ uv run ruff check api/services/blast/capacity_signals.py api/tests/test_blast_capacity_signals.py
All checks passed!
```

## Risks / mitigations

- **Cache key explosion**: keyed only on `(sub, rg, cluster)` — bounded by the
  number of AKS clusters the dashboard ever touched. Acceptable.
- **Stale signals during traffic bursts**: 30 s TTL means a slot just released
  may not show up for up to 30 s. The reservation table is the source of
  truth for slot count; the cached pressure only affects the watermark check.
  Worst case: a single submit waits one extra 30 s cycle.
- **Helper never raises**: an upstream `k8s_*` regression that starts
  returning bogus payloads (not exceptions) could fool the gate into
  always-admit. Stage 1's evaluator has the slot count as the final hard
  guard, so this degrades to "max_slots-only" behaviour rather than a wide
  open admission gate.
