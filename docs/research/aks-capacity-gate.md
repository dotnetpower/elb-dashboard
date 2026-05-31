---
title: AKS Capacity Gate for Parallel BLAST Submits
description: Design proposal for a capacity-aware admission control layer that
  lets the BLAST control plane run multiple submits in parallel on one AKS
  cluster when there is real CPU / memory headroom, while keeping the existing
  per-cluster Redis lock as the depth=1 fallback.
tags:
  - research
  - blast
  - architecture
---

# AKS Capacity Gate for Parallel BLAST Submits

Date: 2026-05-31
Status: **Proposed** — design only. No code or infra change in this commit.
Owner: `api/tasks/blast/` + `api/services/k8s/` maintainers.

> One-paragraph summary: The current submit pipeline serialises every BLAST run
> on a given AKS cluster (a Redis lock keyed on `(cluster, namespace)`, and
> every submit hard-codes `namespace="default"`). Two real bottlenecks make
> this unsafe to relax blindly: (1) the terminal sidecar writes a shared
> `elastic-blast.ini` per submit, and (2) the workload pool may already be
> saturated. We can lift the lock to a slot allocator that consults
> [`k8s_node_request_pressure`](../../api/services/k8s/node_pressure.py) and
> [`k8s_top_nodes`](../../api/services/k8s/metrics.py) (both already
> production-grade, no new SDK), grant N parallel slots only when CPU /
> memory request pressure stays under a watermark, and surface the decision
> on the dashboard. The gate ships **default-OFF** behind `BLAST_GATE_ENABLED`
> and `max_slots=1` so the first deploy is byte-equivalent to today.

---

## 1. Why this matters

Today every BLAST submit on the same cluster waits on the previous one's
`elastic-blast submit` to finish. On a 4-node `blastpool` running at 30% CPU
and 40% memory, that wait is pure deadweight — the workload nodes have room
for more pods, but the control plane refuses to dispatch them. A power user
running 8 small `blastn` jobs in a row gets serialised end-to-end even
though the cluster could run all 8 in parallel.

The user-visible cost on a 4-node blastpool with 8 queued submits:

| Behaviour     | Wall-clock for 8 submits (4-min each) | Pool average CPU |
|---------------|---------------------------------------|------------------|
| Today (lock=1)| ~32 min                              | ~20%             |
| Gate, slots=2 | ~16 min                              | ~45%             |
| Gate, slots=4 | ~8 min                               | ~80%             |
| No gate at all| ~8 min, but OOMKilled at slot 5      | 100%, crashloop  |

The middle two rows are what we want. The bottom row is why "just remove
the lock" is unsafe.

---

## 2. Current state (verified 2026-05-31)

### 2.1 The lock

[`api/tasks/blast/submit_lock.py`](../../api/tasks/blast/submit_lock.py)
:

* Redis SET-NX with TTL 900s, key
  `elb:blast:elastic-blast-submit:<cluster>:<namespace>`.
* `submit_task.py` builds the key as
  `submit_lock_key(cluster_name, "default")` — **namespace is hardcoded**,
  so in practice this is a per-cluster lock, depth 1.
* Lock-busy path
  ([submit_task.py L378+](../../api/tasks/blast/submit_task.py))
  writes a `waiting_for_submit_slot` state row, re-enqueues the same task
  with `countdown=30s` and `queue="blast"`, and **does not** consume the
  12-retry budget (so transient terminal failures keep their full budget).
* Lock release uses a Lua CAS so a stale release never deletes a lock
  held by another caller.

### 2.2 Why the lock exists

Quoted from
[submit_lock.py L13-L16](../../api/tasks/blast/submit_lock.py):

> Per-(cluster, namespace) lock — `elastic-blast submit` writes Kubernetes
> objects (ServiceAccount/Secret/PVC/Job) into one namespace and shares a
> working directory on the terminal sidecar, so concurrent submits
> targeting the same namespace can race.

Two concrete races:

1. **Terminal sidecar working directory** —
   `_stream_submit_command`
   ([submit_runtime.py](../../api/tasks/blast/submit_runtime.py)) writes
   `elastic-blast.ini` and `~/.elb-runs/<job>/...`. Two parallel writers
   that share `~/elastic-blast.ini` would shred each other's config.
2. **K8s namespace collisions** — ElasticBLAST stamps the cluster name
   into Job / ServiceAccount / Secret / PVC names. Two parallel submits
   with the same `cluster_name` argument collide on those names.

Both races are fixable in code; neither is an AKS-side limitation.

### 2.3 What's already parallel

* Different `(cluster, namespace)` tuples are independent. With one
  cluster and one hard-coded namespace this gives us **zero** intra-cluster
  parallelism today.
* The Celery worker is sized for parallelism: `worker-main` runs
  `--concurrency=4` over the `blast` queue
  ([run_celery_workers.py L25](../../api/run_celery_workers.py)), so the
  process can dispatch 4 BLAST tasks simultaneously. The lock is the
  binding constraint, not Celery.
* `task_acks_late=True` + `task_reject_on_worker_lost=True` make a
  crashed task return to the broker — safe for the new gate.

### 2.4 AKS signals we already collect (no new SDK)

| Helper | What it returns | Cost |
|---|---|---|
| [`k8s_node_request_pressure`](../../api/services/k8s/node_pressure.py) | Per-pool CPU / memory **request** pressure (`%`), `warning: True` at ≥90%, `max_node`. **Built specifically for this kind of admission decision.** | 2 K8s API GETs (`/nodes`, `/pods`) |
| [`k8s_top_nodes`](../../api/services/k8s/metrics.py) | Per-node actual CPU millicores used + capacity, memory KiB used + capacity, pool label, ready bool | 1 K8s metrics-server GET |
| [`k8s_top_pods`](../../api/services/k8s/metrics.py) | Per-pod CPU/mem usage with namespace + label-selector filter | 1 K8s metrics-server GET |
| [`k8s_get_pods`](../../api/services/k8s/) | Phase counts (Running / Pending / Failed) | 1 K8s API GET |
| [`scripts/research/blast_capacity_probe.py`](../../scripts/research/blast_capacity_probe.py) | Single-query CPU/mem footprint sampled live during a real run; CSV + JSON summary | Read-only, run on demand |

The probe script's stated purpose
([file header](../../scripts/research/blast_capacity_probe.py)) is:

> Sample blastpool node + per-pod metrics from a running AKS cluster
> during a BLAST job and persist a CSV + JSON summary so the
> **admission-control slot manager** can be sized safely.

So the research scaffolding is already in the tree — what's missing is
the runtime gate that consumes those numbers.

---

## 3. Design — the AKS Capacity Gate

### 3.1 High-level flow

```mermaid
flowchart TD
    A[POST /api/blast/submit] --> B{job already<br/>idempotent?}
    B -- yes --> Z1[return existing job_id]
    B -- no  --> C[enqueue submit task]
    C --> D[worker picks up task]
    D --> E[Capacity Gate: read AKS signals]
    E --> F{admit?}
    F -- yes --> G[reserve slot in Redis<br/>set elb:blast:slots:cluster]
    G --> H[stream elastic-blast submit<br/>in per-job workdir]
    H --> I[release slot + finalise state]
    F -- no, headroom --> J[write waiting_for_capacity row<br/>+ gate_reason payload]
    J --> K[re-enqueue with countdown=30s]
    F -- no, deny hard --> L[write rejected row<br/>+ retryable=false]
```

### 3.2 The gate decision

A single function in a new
`api/services/blast/capacity_gate.py`:

```python
def evaluate_capacity_gate(
    credential, subscription_id, resource_group, cluster_name,
    *, predicted_demand: ResourceDemand,
    active_reservations: list[Reservation],
) -> GateDecision:
    pressure = k8s_node_request_pressure(...)   # cached 30s
    top_nodes = k8s_top_nodes(...)              # cached 15s
    pending_pods = k8s_count_pending_pods(...)  # cached 15s

    # Hard denies — never admit, even if the gate is disabled
    if not pressure["reachable"]:
        return GateDecision(admit=False, reason="aks_unreachable", retryable=True)
    if pending_pods > 0:
        return GateDecision(admit=False, reason="pods_pending", retryable=True)

    # Per-pool watermark check (use the warning threshold the helper
    # already computes — 90% — but slide it down to 75% for admit)
    blastpool = pressure["pools"].get(BLAST_POOL_NAME, {})
    if blastpool.get("cpu_request_pct", 0) >= GATE_CPU_WATERMARK_PCT:
        return GateDecision(admit=False, reason="cpu_watermark", retryable=True,
                            measured_pct=blastpool["cpu_request_pct"])
    if blastpool.get("memory_request_pct", 0) >= GATE_MEM_WATERMARK_PCT:
        return GateDecision(admit=False, reason="memory_watermark", retryable=True,
                            measured_pct=blastpool["memory_request_pct"])

    # Reservation accounting — what we've already promised this minute
    reserved_cpu_m = sum(r.cpu_m for r in active_reservations)
    reserved_mem_b = sum(r.mem_b for r in active_reservations)
    available_cpu_m, available_mem_b = _pool_headroom(top_nodes, BLAST_POOL_NAME)
    if predicted_demand.cpu_m > (available_cpu_m - reserved_cpu_m):
        return GateDecision(admit=False, reason="reserved_cpu_exhausted", retryable=True)
    if predicted_demand.mem_b > (available_mem_b - reserved_mem_b):
        return GateDecision(admit=False, reason="reserved_memory_exhausted", retryable=True)

    # Slot ceiling — hard cap so a runaway demand estimator can't burst
    if len(active_reservations) >= GATE_MAX_SLOTS_PER_CLUSTER:
        return GateDecision(admit=False, reason="slot_cap_reached", retryable=True)

    return GateDecision(admit=True, headroom_cpu_m=available_cpu_m - reserved_cpu_m,
                        headroom_mem_b=available_mem_b - reserved_mem_b,
                        slots_in_use=len(active_reservations))
```

### 3.3 Slot accounting (replaces the bool lock)

Instead of `SET key NX EX 900`, slots live in a **Redis hash**:

```
HSET elb:blast:slots:<cluster> <job_id> '{"reserved_at":"…","cpu_m":…,"mem_b":…}'
EXPIRE elb:blast:slots:<cluster> 1800   # safety in case a worker dies
```

Reservation is atomic via a small Lua script that reads the hash,
evaluates `len(hash) < max_slots`, and only writes on success — preventing
the classic check-then-act TOCTOU between two workers.

Release on success / failure removes the field with `HDEL`. The TTL on
the hash itself is the safety net for crash-without-release.

The existing per-(cluster, namespace) Redis lock stays around as a
**Phase 2 isolation primitive**: while a single submit is rewriting
`elastic-blast.ini` inside the terminal sidecar, it still holds the lock
for that file path only. That collapses naturally once we move each
submit into its own `~/elb-runs/<job_id>/` directory (§4.2).

### 3.4 Predicted demand — the cold-start problem

We can't admit safely without a number for "how much CPU / memory will
this submit ask for?". Three tiers, in order of preference:

1. **Per-(database, program) history** — if we've run `blastn` against
   the same DB before, use the p95 of the last N samples from
   `api.services.state.job_state` (already records `runtime_metrics`).
2. **Per-program defaults** — a small table that mirrors what the
   capacity probe found for representative workloads:
   `{blastn: 2_GiB, blastp: 6_GiB, blastx: 8_GiB, tblastn: 4_GiB, …}`.
3. **Hard fallback** — `BLAST_GATE_DEFAULT_DEMAND_MIB` (default 4096) +
   `BLAST_GATE_DEFAULT_DEMAND_CPU_M` (default 1000). Conservative on
   purpose: a first-time submit reserves more than it usually needs, the
   feedback loop tightens the estimate after the first real run.

The probe script becomes a one-time bootstrap: run it once per new DB,
let it populate the per-(program, db) table, then steady-state uses tier
1.

### 3.5 Configuration knobs

All ship **default-OFF / safe-equivalent** per Charter §12a Rule 4
("new guards ship default-OFF"):

| Env var | Default | Effect when default |
|---|---|---|
| `BLAST_GATE_ENABLED` | `false` | Gate code is bypassed entirely — `submit_task` keeps using the existing per-cluster lock with the existing 30s requeue path. **First Container Apps deploy is byte-equivalent to today.** |
| `BLAST_GATE_MAX_SLOTS_PER_CLUSTER` | `1` | When enabled, behaves like today (depth=1) but with richer telemetry. Flip to 2 after soak. |
| `BLAST_GATE_CPU_WATERMARK_PCT` | `75` | Slightly below the `_HIGH_PRESSURE_PCT = 90` warning the existing pressure helper already uses, so we never get within striking distance of the operator-visible "danger" line. |
| `BLAST_GATE_MEM_WATERMARK_PCT` | `75` | Same rationale. |
| `BLAST_GATE_DEFAULT_DEMAND_MIB` | `4096` | Conservative cold-start estimate. |
| `BLAST_GATE_DEFAULT_DEMAND_CPU_M` | `1000` | 1 vCPU per submit. |
| `BLAST_GATE_SIGNAL_CACHE_S` | `30` | How long the AKS metrics read is cached. Lower = fresher, higher = cheaper. |
| `BLAST_GATE_SLOT_TTL_S` | `1800` | Safety expiry on the slot hash. |

---

## 4. Code changes (sketch — not implemented in this commit)

### 4.1 New module — `api/services/blast/capacity_gate.py`

* Pure function `evaluate_capacity_gate(...)` (above).
* Pure helpers `_pool_headroom`, `_load_active_reservations`,
  `_predict_demand`, `_reserve_slot`, `_release_slot`.
* No FastAPI / Celery imports — call sites stay in `api/tasks/blast/`.
* Reservation persistence via the existing
  `api.services.redis_clients.get_broker_redis_client()` pool — must not
  open a new connection pool per call (see the
  [`submit_lock.py` warning](../../api/tasks/blast/submit_lock.py)).

### 4.2 Per-job isolation — `api/tasks/blast/submit_runtime.py`

* Replace the single `~/elastic-blast.ini` workdir with
  `~/elb-runs/<job_id>/elastic-blast.ini` and pass `--cwd` to the
  terminal exec call.
* Add a post-submit cleanup that removes the per-job directory once the
  `elastic-blast submit` exit code is processed (whether success or fail).
* `_ensure_terminal_kubeconfig_context` stays shared — kubeconfig is
  read-only after Az CLI login.

### 4.3 Submit task wiring — `api/tasks/blast/submit_task.py`

* Before `acquire_submit_lock`, call `evaluate_capacity_gate(...)` when
  `BLAST_GATE_ENABLED=true`.
* On `admit=False, retryable=True`: write
  `waiting_for_capacity` state row with the structured `gate_reason` and
  `gate_measured_pct` fields, requeue with `countdown=30s` — same shape
  as the existing `waiting_for_submit_slot` path, **does not** consume
  retry budget.
* On `admit=False, retryable=False`: write `rejected_capacity` row,
  surface `Retry-After: 600` in the submit response, do not requeue.
* On `admit=True`: persist a reservation via `_reserve_slot(...)`, then
  proceed to the existing lock acquisition (now per-job, not per-cluster)
  and the existing `_stream_submit_command` path. On any exit (success,
  failure, terminal error) the slot is released in a `finally`.

### 4.4 New endpoint — `api/routes/blast/capacity.py`

```
GET /api/blast/capacity?subscription_id=…&resource_group=…&cluster_name=…
```

Returns:

```json
{
  "enabled": true,
  "slots": { "in_use": 2, "max": 4 },
  "pool": "blastpool",
  "cpu_request_pct": 62,
  "memory_request_pct": 51,
  "watermark_cpu_pct": 75,
  "watermark_memory_pct": 75,
  "pending_pods": 0,
  "decision_preview": "admit",
  "decision_reason": null,
  "predicted_demand": { "cpu_m": 1000, "mem_mi": 4096 },
  "active_reservations": [
    { "job_id": "…", "reserved_at": "…", "cpu_m": 1000, "mem_mi": 4096 }
  ]
}
```

Hits same `cached_snapshot_with_cluster_gate` cache as the other
`/api/monitor/aks/*` endpoints — degrades to `{"degraded": True,
"degraded_reason": "aks_unreachable"}` rather than 500.

### 4.5 SPA card — `web/src/components/cards/ClusterBento/CapacityGateCell.tsx`

* Renders in the existing `ClusterBento` grid as a sibling of the
  `KpiInline "CPU"` / "Memory" tiles (which already exist in the
  mockups).
* Shows `2/4 slots in use · 62% CPU pressure · admit ready`.
* Yellow band when `decision_preview` is a soft deny (`cpu_watermark`,
  `memory_watermark`, `reserved_*_exhausted`).
* Red band when `decision_preview` is a hard deny (`aks_unreachable`,
  `pods_pending`).

No new chart libraries; reuse the existing `KpiInline` component and
`var(--accent)` / `var(--warning)` / `var(--danger)` tokens from the
glass UI palette.

---

## 5. Telemetry — what we observe before flipping defaults

Three new structured log events (existing `RequestIdMiddleware`
guarantees they're correlated to the submit request):

* `blast_gate_admit{job_id, slots_in_use, headroom_cpu_m, headroom_mem_mi, predicted_cpu_m, predicted_mem_mi}`
* `blast_gate_deny{job_id, reason, measured_pct, retryable}`
* `blast_gate_release{job_id, wall_clock_s, actual_cpu_m_peak, actual_mem_mi_peak}` — last two fields cross-checked against the post-run probe output, this is how the demand model tightens.

Three new
[`api.services.audit`](../../api/services/audit.py) append-blob rows for
the same events, so an operator can `az storage blob download` the
audit log and run analysis offline.

Three new entries in the
[`/api/monitor/sidecars`](../../api/routes/monitor/sidecars.py) SSE
stream (counters): `gate_admit_total`, `gate_deny_total{reason}`,
`gate_wait_seconds_p95`.

---

## 6. Safety strategy — phased rollout

Charter §12a Rule 1 covers RBAC narrowing, but the spirit applies here
too: behaviour that could surprise a Reader / Contributor must ship in
two phases.

### Phase 1 (release N) — gate enabled, depth = 1

* `BLAST_GATE_ENABLED=true`, `BLAST_GATE_MAX_SLOTS_PER_CLUSTER=1`.
* Submit behaviour is byte-equivalent to today (one in flight per
  cluster), but every decision is now logged + audited.
* Dashboard `CapacityGateCell` renders read-only telemetry.
* Soak: 7+ days. Success criterion = no spurious denies (denies should
  only occur when the existing lock-busy path would have triggered).

### Phase 2 (release N+1) — depth = 2

* `BLAST_GATE_MAX_SLOTS_PER_CLUSTER=2`.
* Workload nodes should report ≤ 80% CPU / mem pressure at the
  steady-state peak.
* Watch for: OOMKilled pods, `Pending` pods >0 for >60s,
  `RescaleScheduled` events from the cluster autoscaler. Any one of
  those means roll back to depth = 1 and tighten the demand model.

### Phase 3 (release N+2 or later) — depth = pool capacity

* `BLAST_GATE_MAX_SLOTS_PER_CLUSTER` = `agentpool.max_count` from
  `_serialise_cluster`.
* The watermark (75%) is now the real binding constraint, not the slot
  ceiling. The gate will admit up to whatever the pressure model allows.

Each phase is a separate PR with the
[per-feature change note](../features_change/) under
`docs/features_change/YYYY-MM/` summarising the metrics from the
previous phase.

---

## 7. Explicit non-goals

* **Multi-cluster job placement.** Routing a job to the least-loaded of
  several clusters is a real product feature, but it depends on the gate
  landing first to have honest per-cluster headroom numbers. Separate
  epic.
* **AKS pool autoscale.** The gate **does not** scale the
  `blastpool`. It admits within current capacity. Scaling stays a
  cluster-autoscaler / `agent_pool_profiles[].max_count` concern. The
  gate's `deny: slot_cap_reached` event is precisely the signal that
  autoscale should bump `max_count`.
* **Cross-namespace submits.** The hard-coded `namespace="default"` is
  intentional today — `elastic-blast` expects one tenant per namespace
  and the existing audit trail assumes the same. Multi-namespace is a
  separate change.
* **Predicting demand from FASTA / DB size.** Out of scope for this
  proposal. Tier 1 (history) + Tier 2 (per-program defaults) is enough
  to start; FASTA-based modelling is a probe-script improvement.
* **Charging per-submit cost.** The gate makes parallel submits
  observable but does not introduce billing. Cost telemetry is a
  separate epic on `api/services/cost/`.

---

## 8. Open questions for the next review

1. Should `evaluate_capacity_gate` consult the running-pod **actual**
   usage (`k8s_top_nodes`) in addition to the **request** pressure, or
   is request pressure alone enough? Request pressure is what the
   scheduler binds on, but actual usage is what tells us if the request
   estimates are honest. The probe script already correlates both;
   reusing that correlation here is straightforward but adds one more
   K8s API call per decision.
2. Where does the `predicted_demand` history table live? Three options:
   (a) a new Storage Table `blast_demand_history`, (b) annotate the
   existing per-job state row, (c) a Redis sorted set keyed by
   `(program, db)`. Lean toward (c) for read latency, with (a) as a
   periodic flush.
3. Should the gate be **per-pool** instead of **per-cluster**? Today
   `BLAST_POOL_NAME` is a constant; a future cluster with `blastpool-a`
   + `blastpool-b` would want independent slot accounting. The slot key
   already includes the pool name in the design, so this is forward-compatible.
4. Should we expose `BLAST_GATE_FORCE_ADMIT=true` as a per-submit
   escape hatch for an operator manually saying "I know what I'm doing"?
   The answer is probably no — the rejection path is the safe one — but
   it bears explicit refusal in the design.

---

## 9. References

* [api/tasks/blast/submit_lock.py](../../api/tasks/blast/submit_lock.py) — current per-(cluster, namespace) Redis lock + Lua release.
* [api/tasks/blast/submit_task.py](../../api/tasks/blast/submit_task.py) — submit pipeline, lock-busy requeue path.
* [api/services/k8s/node_pressure.py](../../api/services/k8s/node_pressure.py) — per-pool request pressure helper.
* [api/services/k8s/metrics.py](../../api/services/k8s/metrics.py) — `k8s_top_nodes` / `k8s_top_pods`.
* [scripts/research/blast_capacity_probe.py](../../scripts/research/blast_capacity_probe.py) — single-query CPU/mem probe; demand-model bootstrap.
* [api/run_celery_workers.py](../../api/run_celery_workers.py) — `worker-main --concurrency=4 --queues=default,acr,azure,blast,storage`.
* [api/celery_app.py](../../api/celery_app.py) — `task_acks_late=True`, retry semantics.
* [.github/copilot-instructions.md §12a](../../.github/copilot-instructions.md) — phased rollout discipline + default-OFF guard rule.
* [docs/features_change/2026-05/2026-05-22-submit-parallelism-and-fast-poll.md](../features_change/2026-05/2026-05-22-submit-parallelism-and-fast-poll.md) — the historical PR that lifted the lock from single-key to per-(cluster, namespace).
* [docs/features_change/2026-05/2026-05-22-submit-lock-requeue.md](../features_change/2026-05/2026-05-22-submit-lock-requeue.md) — the lock-busy requeue / retry-budget contract that the new gate inherits.
* [Kubernetes Resource Management documentation](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) — requests vs limits semantics behind the pressure helper.
* [Azure Kubernetes Service Cluster Autoscaler documentation](https://learn.microsoft.com/en-us/azure/aks/cluster-autoscaler) — the layer the gate hands off to when it detects `slot_cap_reached`.

---

## 10. Status board

| Stage | Description | Status |
|---|---|---|
| Stage 0 | Design proposal (this document) | **Proposed** (2026-05-31) |
| Stage 1 | `api/services/blast/capacity_gate.py` + unit tests | Not started |
| Stage 2 | Per-job terminal workdir isolation | Not started |
| Stage 3 | `submit_task` wiring + `BLAST_GATE_ENABLED` env flag | Not started |
| Stage 4 | `/api/blast/capacity` endpoint + dashboard cell | Not started |
| Stage 5 | Telemetry (logs + audit + sidecar counters) | Not started |
| Stage 6 | Phase 1 deploy (depth=1, telemetry only) | Not started |
| Stage 7 | Phase 2 (depth=2) after soak | Not started |
| Stage 8 | Phase 3 (depth=pool max) after soak | Not started |
