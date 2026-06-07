---
title: Auto-warmup "cluster starts but stays cold / Stale" root cause + reconcile critique
description: Fix the forced re-warm intent being lost after AKS start and the in-flight lock starving warmup retries, plus a 32-item critique of the auto-warmup reconcile path with severity and status.
tags:
  - blast
  - operate
  - architecture
---

# Auto-warmup "cluster starts but stays cold / Stale" — root cause + reconcile critique

## Motivation

Users reported that after an AKS cluster starts, the configured databases do not
auto-warm and the dashboard keeps showing them `Stale` (ephemeral clusters) or
`Ready` but with cold RAM and slow searches (`node_disk` clusters). The failure
is persistent — a fresh start does not recover it.

## Root causes (two, both fixed)

### RC1 — the forced re-warm intent is lost (one-shot fires too early)

`start_aks` enqueues a single `reconcile_auto_warmup` with `force=True`
*immediately* after `begin_start().result()`. At that moment the AKS control
plane is back but the `blastpool` nodes have not finished registering `Ready`,
so `auto_warmup_ready_gate` returns `waiting_for_warmup_nodes` and the reconcile
returns **without enqueuing any warmup** — the `force=True` intent is silently
dropped.

The only reconcile that later observes the nodes `Ready` is the recurring beat
tick, which runs with `force=False`. Consequences:

- **`node_disk` clusters**: VMSS instance names stay stable across
  `az aks stop`/`start`, so the pre-stop `warm-<db>-<shard>` Jobs are not flagged
  `Stale` and the DB still reports `Ready`. The un-forced beat tick hits the
  `warm_state in {Ready, Loading} and not force` skip → the DB is **never
  re-warmed**, leaving the node RAM page cache cold forever.
- **ephemeral clusters**: usually self-heal (node rotation flips `Stale` and the
  un-forced tick re-enqueues), but the forced path that exists specifically to
  cover the cold-RAM case never actually runs.

**Fix**: persist a `force_rewarm_pending` flag on the `AutoWarmupPreference`.
`start_aks` sets it `True`; the beat reconcile computes
`effective_force = force or pref.force_rewarm_pending` and only clears the flag
once a warmup has actually been **enqueued** (gate satisfied). The forced intent
now survives across beat ticks until the cluster is genuinely workload-ready.

### RC2 — the in-flight lock starves warmup retries

`autowarmup_inflight_acquire` does a Redis `SET NX EX` with a **15-minute** TTL
and there was **no release path** anywhere. A warmup that defers (nodes not all
ready) or fails (network/RBAC) holds the slot for the full 15 minutes, so every
120 s beat tick for that DB is skipped with `reason: "inflight"` and the DB stays
`Stale` far longer than necessary — a failed warmup effectively retries only once
per 15-minute window.

**Fix**: add `autowarmup_inflight_release`, call it from `warmup_database`'s
`finally` (auto-warmup path only, gated by the new `release_inflight_on_done`
kwarg the reconcile sets), and shorten the TTL from 15 → 8 minutes as a backstop.
A deferred/failed warmup is now retried on the next beat tick.

## API / behaviour changes

- `AutoWarmupPreference` gains `force_rewarm_pending: bool = False`
  (round-trips through `to_dict`/`from_dict`; default `False` for legacy rows).
- `mark_auto_warmup_ready_state(..., clear_force_pending: bool = False)`.
- `auto_warmup_reconcile`: `effective_force`, clears the pending flag only when a
  warmup was enqueued, sets `release_inflight_on_done=True` on the warmup enqueue,
  sanitises the per-pref `error` field.
- `warmup_database(..., release_inflight_on_done: bool = False)` releases the
  in-flight slot in `finally`.
- `start_aks` persists `force_rewarm_pending=True` and no longer logs raw
  exception objects for the best-effort enqueues.

## Validation

- `uv run pytest -q api/tests/test_auto_warmup.py` — 24 passed (5 new:
  `test_to_dict_round_trips_force_rewarm_pending`,
  `test_force_rewarm_pending_honoured_by_unforced_reconcile`,
  `test_force_rewarm_pending_kept_when_gate_not_ready`,
  `test_autowarmup_inflight_release_deletes_key`,
  `test_autowarmup_inflight_release_noop_without_redis`).
- `uv run pytest -q api/tests` — 3061 passed, 3 skipped.
- `uv run ruff check api` — clean on touched files.

## Critique — 32 issues in the auto-warmup reconcile path

Severity + status. **Fixed** items shipped in this change; **Open** items are
recommended follow-ups (left open deliberately to keep this change reviewable and
low-risk — several touch the stale-detection / node-readiness contracts).

### Critical / High — root causes

1. **[Fixed]** RC1: one-shot `force=True` reconcile fires before nodes Ready →
   forced re-warm dropped → `node_disk` DB stays `Ready` but RAM-cold forever.
2. **[Fixed]** RC2: 15-min in-flight lock with no release starves retries of
   deferred/failed warmups.

### High — structural

3. **[Open]** `auto_warmup_ready_gate` requires **all** expected nodes Ready with
   no upper time bound. One node that never becomes Ready (quota, spot eviction,
   ImagePullBackOff) blocks warmup for that cluster forever with no escalation
   and no partial warm.
4. **[Open]** `expected_warmup_node_count` trusts `pref.num_nodes`; if it exceeds
   the count the cluster actually brings up (or the pool autoscaled down), the
   gate can never be satisfied → permanent wait. No reconciliation against the
   pool's live max.
5. **[Open]** `require_all_warmup_nodes=True` is hard-coded; there is no fallback
   to warm the ready subset, so a single bad node blocks every DB on the cluster.
6. **[Open]** Managed-VNet AKS clusters (no BYO subnet) hit a Storage `403`
   network block on warmup azcopy; the reconcile re-enqueues a doomed warmup every
   retry with only a generic warning. No detection/short-circuit of the
   network-block failure class.
7. **[Open]** `warmup_database` does not distinguish transient (retryable) vs
   permanent (config/network) failures; Celery `max_retries=2` burns retries on
   permanent failures, then the beat re-enqueues indefinitely.
8. **[Open]** No circuit breaker: a DB that fails every time is re-enqueued every
   8 min forever, generating endless App Insights failures.
9. **[Open]** `_mark_stale_warmup_nodes` flips to `Stale` on **any** shard whose
   pinned node is momentarily NotReady — no debounce/grace → spurious Stale →
   re-warm churn.
10. **[Open]** The reconcile processes up to 500 prefs serially in one tick, each
    making 3+ network calls; a slow tick can exceed the 120 s beat interval. No
    per-pref timeout, parallelism, or single-flight across overlapping ticks.
11. **[Open]** `reconcile_auto_warmup` has no `expires`/overlap guard; a long tick
    can run concurrently with the next scheduled tick (only the in-flight lock,
    now shortened, prevents double-enqueue).

### Medium

12. **[Open]** `start_aks` enqueues the one-shot reconcile to the `storage` queue
    while the beat reconcile uses the `reconcile` queue; a backed-up `storage`
    queue delays post-start warmup. Inconsistent routing.
13. **[Fixed]** `start_aks` logged the full exception object for best-effort
    enqueues (potential ID leak) → now logs `type(exc).__name__`.
14. **[Open]** If the worker dies between `begin_start` success and the
    enqueues, the OpenAPI deploy side effect has no reconciler to retry it
    (warmup is now partially covered by `force_rewarm_pending`).
15. **[Open]** Forced re-warm calls `k8s_release_warmup_cache` (full delete)
    before re-ensure; between delete and ensure the DB briefly shows no warm Jobs
    (dashboard flicker; a concurrent search could land on a cold node).
16. **[Open]** Releasing the in-flight lock on the *deferred* path lets the next
    beat re-enqueue a fresh JobState row each tick while nodes are partially
    ready (mitigated by the gate, but unguarded).
17. **[Open]** `_seed_auto_warmup_job_state` creates a `warmup` JobState row per
    enqueue with no GC; repeated failures accumulate rows unbounded.
18. **[Open]** `job_id` uses `int(time.time())`; two ticks in the same second
    could collide (low probability, unbounded).
19. **[Open]** `mark_auto_warmup_ready_state` does a full CAS read+write every
    tick for every pref even when nothing changed → needless Table writes / ETag
    churn / throttle risk.
20. **[Open]** `_latest_ncbi_source_version()` HTTP lookup is on the beat-tick
    critical path; a slow NCBI endpoint slows every reconcile.
21. **[Open]** The `update_required` skip surfaces no actionable signal to the
    user — the DB just "doesn't warm" with the cause buried in an internal
    `skipped` reason.
22. **[Open]** During a node-image upgrade, cordoned nodes drop out of the ready
    set while `cluster.node_count` stays high → gate blocks warmup for the whole
    upgrade window with no messaging.

### Low / hygiene

23. **[Open]** The 1.5 s Redis timeout on in-flight acquire is per-DB on the
    reconcile critical path; many DBs × 1.5 s adds up when Redis is degraded.
24. **[Open]** The warmup outer `except` logs `"warmup verification failed"` for
    *all* failures including node-warm-phase failures — misleading.
25. **[Open]** `max_retries=2` + `retry_backoff_max=300` overlaps Celery's own
    retry with the next beat enqueue (after RC2's release) → possible duplicate
    concurrent warmups.
26. **[Open]** A user PUT to `/api/warmup/auto-preference` clears
    `force_rewarm_pending` (default `False` in `normalise_preference`) — editing
    prefs right after start can drop a pending force. Accepted trade-off.
27. **[Open]** No telemetry counter for reconcile outcomes
    (triggered/waiting/failed) — operators must log-spelunk for warmup health.
28. **[Fixed]** The per-pref `error` surfaced in the reconcile result was a raw
    exception string (could carry IDs) → now `sanitise(...)`.
29. **[Open]** `cluster_is_workload_ready` checks the *configured* `agent_pool.count`,
    not the running count; it relies on ARM `power_state` which lags actual node
    readiness.
30. **[Open]** The gate read and the enqueue are not transactional with a
    concurrent `enabled=False` toggle (partially mitigated by CAS in
    `mark_ready`).
31. **[Open]** `k8s_warmup_status` is fanned out by both the monitor poll (every
    few seconds) and the reconcile (every 120 s) with no shared cache → duplicate
    K8s load.
32. **[Open]** No reconciler detects a warmup stuck in `Loading` (hung vmtouch /
    image pull); the un-forced tick skips it as `Loading` forever (RC1's
    `effective_force` helps only when a force is pending).

## Follow-up

The Open items above are tracked for a subsequent change. The highest-value next
steps are #3/#4/#5 (bounded readiness gate + ready-subset fallback) and #8
(circuit breaker for permanently failing DBs), since those are the remaining
"never converges" failure modes after RC1/RC2.
