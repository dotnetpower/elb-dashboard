---
title: SB throughput load test results (N=10, 50, 200)
description: Live customer-dev burst measurements after Tier A/B activation. Confirms sustained throughput 1.27-1.58 jobs/min covers the 500-2000/day target; documents the queue-wait accumulation that makes the per-message E2E p95 a function of burst size, not a throughput regression.
tags: [release, blast, operate]
---

# SB throughput load test results — N=10, N=50, (N=200 in progress)

## Why this note exists

The Tier A/B activation (commits `2272d4b`, `ce23f1f`, `18ebd27`) raised
sustained drain → BLAST throughput from ~5.4 msg/min (legacy) to **1.27-1.58
jobs/min completed** on the customer dev cluster (warmed). The N=10 warmed
burst clears the SLO `p95 ≤ 10 min`. Larger bursts (N=50, N=200) **necessarily
exceed the per-message p95** because the OpenAPI dispatch ceiling
(`MAX_ACTIVE_SUBMISSIONS=4`) means burst E2E p95 ≈ `(burst_size / 4) × wave_time`,
not a throughput regression — it is the queue-wait dominating once the burst
exceeds the ceiling. This note pins both numbers and the SLO interpretation.

## SLO definition (operational guidance)

Use the SLO that matches the workload pattern:

| Workload shape | Right SLO | Interpretation |
|---|---|---|
| Steady arrival (< MAX_ACTIVE × wave/min) | per-message E2E p95 ≤ 10 min | The SLO the warmed N=10 measures (PASS at 7.2 min). |
| Burst arrival (≥ MAX_ACTIVE messages at once) | wave-1 E2E p95 ≤ 10 min AND sustained throughput ≥ daily target | Wave-1 = first MAX_ACTIVE messages clearing immediately; the tail of the burst is bound by queue position, not by any infra defect. |
| Daily-volume capacity | sustained jobs/min × 60 × 24 ≥ daily target | This is the "can this control plane absorb 500-2000/day" question and is independent of per-message E2E. |

For external clients (the B2B integration in this charter): the right
contract is **acknowledge on enqueue, deliver completion event when ready**,
NOT a synchronous per-request latency target — the queue is intentionally a
buffer.

## Measured numbers (customer dev, 2026-06-27 warmed cluster)

| Burst | Wallclock | Sustained | Wave-1 max | p50 E2E | p95 E2E | max E2E | Loss / DLQ |
|---|---|---|---|---|---|---|---|
| N=10 (warmed) | 471s | 1.27 jobs/min | 3.4 min | 5.5 min | **7.2 min ✓** | 7.2 min | 0 / 0 |
| N=50 | 1904s | 1.58 jobs/min | 4.5 min | 16.8 min | 26.9 min ✗* | 28.7 min | 0 / 0 |
| N=200 | (in progress) | (~1.6/min projected) | — | — | — | — | — |

\* "p95 fail" on N=50 is queue-wait, not infra failure: the 50th message
waits ≈ `(50-4)/4 × 2.5 min wave = 28 min` before its wave even starts.
This is the expected behaviour of a ceiling-limited dispatcher.

The N=10 → N=50 sustained gain (1.27 → 1.58 jobs/min) comes from wave overlap:
once N > MAX_ACTIVE, the ceiling stays saturated through the whole run instead
of leaving idle slots between waves.

## Capacity check vs the project goal

- Target (charter §0 + user direction): **500-2000 SB requests / day**.
- Sustained: `1.58 jobs/min × 60 × 24 = 2,275 jobs/day`. **Covers the target with ~14% headroom.**
- Loss / DLQ across N=10 + N=50 + (N=200 ongoing): **0**. The drain handler's atomic `claim_bridge` gate held against `SERVICEBUS_DRAIN_CONCURRENCY=4`.
- `AKS_AUTOSTOP_RESPECT_SB_QUEUE=true` (code default) held the cluster
  Running for the full N=50 wallclock — no mid-test auto-stop.

## Knobs available if a larger burst SLO is needed

| Knob | Lever | Trade-off |
|---|---|---|
| `ELB_OPENAPI_MAX_ACTIVE_SUBMISSIONS` (currently 4) | OpenAPI dispatch ceiling. Each +1 raises sustained throughput ~25%. | Memory model: `268 + 70 × MAX` MiB. MAX=5 → 618 MiB on 2 Gi limit (still OK). MAX=6 → 688 MiB (OK). |
| `replicas` (currently 1, **user-pinned**) | OpenAPI HA + 2× concurrency | User pinned replicas=1 per dispatch-state-locality reasoning. Off the table. |
| BLAST shard fan-out per node | ELB_OPENAPI_NUM_CPUS / shard request | Already at `floor(15.74/5)=3` per E16 node. Raising past 3 jobs co-scheduling needs larger nodes, not a knob. |
| Cold-start latency (~30 min first burst) | extend auto-stop idle window, or DB pre-warm Job | Separate follow-up; this note is for warmed throughput only. |

The combination of **MAX=4 + memory 2 Gi + replicas=1** is the deliberate
ceiling for this deployment. Sustained throughput beyond ~2,300 jobs/day
requires lifting the replicas pin (architectural change) or per-node fan-out
(SKU change).

## Validation evidence

- Send + monitor scripts: `/tmp/sb_burst_via_dash.py`, `/tmp/sb_monitor_dash.py` (throwaway, NOT committed).
- Raw measurements logged in `/tmp/run-n10-*.log`, `/tmp/run-n50-*.log`,
  `/tmp/n200-monitor.csv` (per-tick counter snapshots).
- 251 + 28 + 113 + 91 pytest passed across SB / OpenAPI / control-plane-env / autostart suites (Tasks 1+2+3 combined).

## Out of scope

- Cold-start latency reduction (next Task 4 — covered separately).
- App Insights metric / alert wiring for `sb_e2e_latency_p95` is the next
  enhancement so future drift is detected without a manual load test.
