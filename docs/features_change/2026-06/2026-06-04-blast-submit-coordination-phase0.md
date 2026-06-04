---
title: BLAST submit coordination — Phase 0 (k8s-backed admission gate, default-OFF)
description: Cluster-authoritative submit serialization (Lease) and run-concurrency ceiling (finalizer Job count) behind BLAST_COORD_BACKEND=k8s, default-OFF.
tags:
  - blast
  - architecture
---

# BLAST submit coordination — Phase 0

## Motivation

The single Celery worker in the `ca-elb-dashboard` revision serializes dashboard
submits in-process, but that lock is **not** visible to the on-AKS `elb-openapi`
submit path, and the in-revision Redis broker is loopback-only (not reachable
from pods). So two independent submitters can race, and nothing caps the number
of concurrently *running* BLAST jobs against finite AKS capacity. The truth for
coordination must therefore live in the cluster (AKS etcd), not in the
dashboard process.

Phase 0 introduces a **cluster-authoritative** admission gate, shipped
**default-OFF** behind `BLAST_COORD_BACKEND=k8s` (Charter §12a Rule 4). With the
flag unset the submit path is byte-equivalent to the legacy
`BLAST_GATE_ENABLED` / submit-lock behaviour.

## User-facing change

When `BLAST_COORD_BACKEND=k8s` is set on the api/worker sidecars:

- **Gate A (submit mutex)** — a Kubernetes `Lease`
  (`coordination.k8s.io/v1`, `elb-blast-submit-<namespace>`,
  `leaseDurationSeconds=900`, resourceVersion CAS) serializes submits to one at
  a time. A job that cannot get the Lease is requeued (`phase=waiting_for_submit_slot`,
  `countdown=30s`) until a bounded deadline.
- **Gate B (run-concurrency ceiling, default 3)** — counts DISTINCT
  `elb-job-id` among non-terminal `app=finalizer` Jobs. Over the ceiling →
  requeue (`phase=waiting_for_capacity`). Fail-closed: a count error releases the
  Lease and requeues rather than admitting blindly.
- Both gates are per-namespace (`default`). The Lease is released in a `finally`
  block after the submit completes; a hard SIGKILL leaks it only until the 900s
  TTL, and a startup invariant assertion guarantees `submit_exec(600s) <
  soft_time_limit < hard_time_limit` and `submit_exec < lease_ttl` so a submit
  can never outlive its own Lease silently.
- The split (sharded) fan-out gates each child inline via
  `wait_for_k8s_admission` (bounded by a small per-child cap AND a single parent
  wall-clock budget — see the hardening follow-up below), since children
  dispatch sequentially within one parent task and cannot Celery-requeue
  mid-fan-out.

§2a precedence: `BLAST_COORD_BACKEND=k8s` wins over `BLAST_GATE_ENABLED` — the
Redis capacity gate and submit lock are bypassed entirely (`reserve_slot` /
`acquire_submit_lock` never run).

## ⚠️ Production-enablement precondition (critique C1)

**Do NOT set `BLAST_COORD_BACKEND=k8s` in a deployed environment until the
sibling `dotnetpower/elastic-blast-azure` on-AKS submit path acquires the SAME
Lease (`elb-blast-submit-<namespace>`) before it runs `elastic-blast submit`.**

Phase 0 only serializes the **dashboard** submit path against itself. The
headline "serializes both paths" is true *only* once the sibling repo also takes
the Lease — until then, enabling the flag gives a false sense of mutual exclusion
because the on-AKS path can still submit concurrently with a dashboard submit
that holds the Lease. The flag stays default-OFF precisely so this precondition
is met first (tracked cross-repo per Charter §13).

## Critique-hardening follow-up (2026-06-04)

A 26-item design critique drove the following Phase-0 hardening (all still
default-OFF, no behaviour change when `BLAST_COORD_BACKEND` is unset):

- **Fail-closed Lease liveness (M16)** — a *present-but-unparseable* `renewTime`
  is now treated as **held** (not taken over); only a truly absent `renewTime`
  key counts as available. Prevents two submitters racing when the sibling repo
  writes a timestamp format we don't recognise.
- **Forbidden surfaced loudly (H7)** — a `401/403` on the Lease GET (admin
  kubeconfig rejected → credential rotation / apiserver network fault, NOT lock
  contention) raises a clear `SubmitLeaseApiError` instead of looping forever.
- **Gate A/Gate B namespace consistency (C3)** — the Gate B count now scopes to
  the same raw namespace the Lease locks, instead of a `_namespace_or_default`
  cluster-default lookup that could resolve to a divergent namespace and enforce
  the ceiling against the wrong population.
- **Label-less finalizer fail-closed count (M15)** — a non-terminal
  `app=finalizer` Job missing the `elb-job-id` label now counts as one occupied
  slot (synthetic per-Job key) instead of being skipped (which under-counted and
  over-admitted past the ceiling).
- **Split fan-out head-of-line bound (H4/H5)** — each split child now waits at
  most `BLAST_SPLIT_CHILD_GATE_WAIT_MAX_SECONDS` (default **300s**, far below the
  1800s requeue-path budget), and the whole fan-out is bounded by a single
  `BLAST_SPLIT_PARENT_GATE_BUDGET_SECONDS` (default 1800s) wall-clock budget
  computed ONCE before the loop, so a Celery retry of the parent cannot reset
  each child's wait window and monopolise the single worker.
- **Requeue de-sync jitter (H6/L25/L26)** — the gate-deny `countdown` adds
  +0..10s jitter around the 30s base (single source) and the inline split-wait
  sleep is jittered ±20%, so a herd of submitters denied at the same instant do
  not re-poll the apiserver in lock-step.
- **Explicit deadline carry (M11)** — the requeue now carries both wait
  deadlines via explicit locals instead of relying on dict-key-overwrite order.
- **Single exec-timeout source (M19)** — the submit/split `terminal_exec`
  timeout is now imported from `SUBMIT_EXEC_TIMEOUT_SECONDS` (one constant) at
  both call sites instead of a literal `600`.
- **Invariant parses Celery limits unclamped (M18)** — `assert_coordination_invariants`
  now parses `CELERY_TASK_*_TIME_LIMIT` with a bare `int()` (matching
  `celery_app`), so it validates the exact numbers the worker runs with rather
  than a clamped value.

### Known Phase-0 tradeoffs (deferred by design)

These are documented rather than fixed in Phase 0 because the correct fix is
heavier than the default-OFF gate warrants; they are revisited before the flag
flips ON:

- **Over-admit-on-lag (C2)** — the finalizer becomes visible to Gate B only
  after the `elastic-blast submit` subprocess returns; the Lease is released at
  the same point. The `finalizer_grace_seconds` window mitigates the async
  batch-creation lag but does not fully close a sub-second admit overlap. Proper
  fix (count a "reserved" marker written *before* release) is Phase 1.
- **No job-id idempotency fencing (M12)** — a duplicate submit of the same
  `job_id` is not fenced at the Lease layer; the UI/route layer is expected to
  dedupe. Phase 1 may add a fencing token.
- **Split partial-failure compensation (M13)** — when some split children fail
  the gate, already-submitted siblings are not rolled back; the parent reports
  partial failure and leaves cleanup to the operator.
- **Late results-export finalizer undercount (M14)** — a finalizer that a
  results-export step recreates late could momentarily undercount; bounded by
  the same grace window.
- **Grace (300s) vs Lease TTL (900s) window (M20)** — a phantom finalizer past
  grace but within TTL is not counted; acceptable because the Lease still
  serialises submits during that window.

### Critique items re-classified as non-issues

- **Per-poll token storm (L23) — invalid.** K8s sessions AND admin credentials
  are pooled with a 5-min TTL in `api/services/k8s/client.py`, so a poll loop
  does not mint a new `listClusterAdminCredential` per iteration.
- **RBAC-403 capability probe (H8) — intentionally skipped.** The Lease verbs
  run under the admin kubeconfig (`list_cluster_admin_credentials`), which
  **bypasses Kubernetes RBAC**, so a Role-binding probe would test nothing the
  manifest-deploy path doesn't already exercise. A real Lease probe would have to
  *mutate* cluster state (create a probe Lease), violating the probe's
  read-only/no-mutate contract, so it is deliberately not added.

## Critique round 3 — broad/cold audit, 20+ findings (2026-06-04)

A colder, wider self-critique across new scenarios (auth asymmetry,
partial-failure/orphaning, namespace split-brain, fail-open counting,
exception-masking, observability) surfaced two genuine **HIGH** defects plus a
set of MEDIUM/LOW hardening items. Only real defects were changed; verified
non-issues are recorded so they are not re-litigated.

### Fixed — HIGH

- **H-A — split child orphaned + siblings abandoned on exec raise.**
  `_dispatch_split_child_submits` set each child to `status="running"` *before*
  the gate, then ran `terminal_run(...)` inside a bare `try/finally`. A raised
  `TerminalExecError` (timeout, exec-server down, `az`/`kubectl` error)
  propagated out of the function, **abandoning every subsequent shard** and
  leaving the failing child stuck at `running` forever. Fix: wrap the exec in
  `try/except/finally` — the exception now fails ONLY that child
  (`error_code=terminal_exec_unavailable`, audit history `submit_exec_error`),
  the loop continues to the next shard, and the `finally` still releases the
  shard's Lease. Test:
  `test_dispatch_split_child_submits_raised_exec_fails_child_continues`.
- **H-B — Gate A / Gate B credential asymmetry.** Gate A (the Lease,
  `api/services/k8s/submit_lease.py`) acquires via the RBAC-bypassing **admin**
  kubeconfig, while Gate B (`k8s_count_active_blast_submissions`) listed Jobs via
  the **non-admin** MI token. On a cluster where the shared MI lacks `jobs:list`
  RBAC, Gate B's list `403`s → the fail-closed contract requeues **every**
  submit forever even though Gate A keeps admitting — a cluster-wide submit
  outage from an auth split. Fix: Gate B now reads under `admin=True`, the same
  cluster-truth credential as the Lease. Existing count tests stay green (they
  stub `_get_k8s_session`).

### Fixed — MEDIUM

- **M-A — namespace split-brain (latent).** `submit_task` hard-coded
  `namespace="default"` while `_dispatch_split_child_submits` took a
  `namespace: str` parameter — two independent literals that could drift so the
  two mutexes lock *different* Leases and never exclude each other. Fix: a single
  `SUBMIT_COORDINATION_NAMESPACE = "default"` constant in `coordination.py` now
  feeds both paths.
- **M-B — fail-OPEN terminal count.** Gate B excluded any finalizer with
  `status.failed > 0` as "terminal". But `failed` increments per failed Pod
  attempt while the Job controller is *still retrying* (backoffLimit not
  exhausted) → a retrying finalizer was dropped from the count → under-count →
  over-admit past the ceiling. Fix: a count-local `_finalizer_is_terminal`
  treats a finalizer as terminal ONLY when definitively done (`succeeded>0`,
  `completionTime` set, or a `Complete`/`Failed` condition `True`); a bare
  `failed>0` with no terminal condition is counted (fail-closed). The shared
  `_job_is_terminal` (used by `k8s_check_blast_status`) is left unchanged to
  avoid broad regressions. Tests: `test_retrying_finalizer_counted`,
  `test_completion_time_is_terminal`, updated `test_skips_terminal`.
- **M-D — release in `finally` could mask the real exception.** The submit_task
  `finally` re-called `get_credential()` for the Lease release; if that raised it
  would mask the original submit exception being unwound. Fix: reuse the
  credential captured at acquire time and wrap the release so a best-effort
  failure logs and never escapes `finally`.

### Fixed — LOW

- **L-A — advertised vs actual requeue delay mismatch.** The state row wrote
  `retry_after_seconds=30` (the bare base) while the task re-enqueued with the
  jittered 30–40 s countdown. Fix: compute the jittered countdown once and use
  the same value for both the row and the Celery `countdown`.

### Verified non-issues / deferred (recorded, no change)

- **M-C (regular gate-deny audit parity)** — the regular path already emits an
  `event="k8s_gate_deny"` state event with `error_code`; this is the audit-trail
  equivalent of the split path's `append_history`, so no change is needed.
- **Pooled credentials** — admin kubeconfig + sessions are pooled (5-min TTL) in
  `api/services/k8s/client.py`, so per-poll token storms are a non-issue.
- **Frontend deny phases** are status-keyed (Running/Failed) in the SPA, so the
  `waiting_for_*` phase strings are cosmetic, not a state-machine surface.
- **Lease CAS without `resourceVersion`** and **second-409 release with no log**
  are residual LOW edges retained as-is: both are best-effort paths whose worst
  case is one extra retry, well within Phase-0 risk tolerance.

After these fixes the worst remaining severity is LOW.

## Critique round 2 — split fan-out Gate B self-deadlock (2026-06-04)

A second design-critique pass on the round-1 hardening found one **HIGH** defect
and two **MEDIUM** liveness tightenings, all still default-OFF:

- **HIGH — split fan-out self-deadlock via Gate B.** The split parent dispatches
  shards sequentially inside one task, and each shard's `app=finalizer` Job is
  lifecycle-long. Routing every shard through the FULL gate (Gate A + Gate B)
  meant a split with more shards than the run-concurrency ceiling (default 3)
  would **deadlock on itself**: the earlier shards permanently occupy the slots
  the later shards block on, so every shard past the ceiling waits its whole
  per-child cap and then FAILS — and meanwhile a single multi-shard split could
  starve unrelated submitters out of all ceiling slots. **Fix:** split children
  now take **Gate A only** (`acquire_k8s_admission(..., check_capacity=False)` /
  `wait_for_k8s_admission(..., check_capacity=False)`). They still serialise
  through the submit Lease against every other submitter, but are not counted
  against the Gate B ceiling. A split is one logical submit; the split planner —
  not Gate B — bounds shard concurrency. Gate B continues to bound distinct
  submit *jobs* on the regular path (unchanged).
- **MEDIUM — wait loop took the Lease one extra time past the deadline.**
  `wait_for_k8s_admission` now pre-checks the deadline at the TOP of the loop, so
  an already-expired wait raises `K8sGateWaitTimeout` **before** issuing another
  submit-Lease CAS write (a wasted apiserver write that briefly blocks live
  submitters).
- **MEDIUM — retry sleep could overshoot a near deadline.** The jittered retry
  nap is now clamped to the remaining budget (`min(nap, remaining)`), so the last
  poll lands at the deadline instead of up to ~6s past it.
- **LOW (observability) — split-child `error_code` was a human sentence.** The
  state-row `error_code` is now a greppable `blast_submit_gate_unavailable`; the
  human-readable cause (`parent_gate_budget_exhausted` /
  `submit_lease_unavailable`) moves to the history `detail` field.

New regression tests in `api/tests/test_blast_k8s_gate.py`:
`test_gate_a_only_admits_without_counting` (Gate B never consulted),
`test_gate_a_only_still_busy_when_lease_held` (Gate A still serialises),
`test_wait_forwards_check_capacity`, `test_wait_predeadline_raises_without_acquire`
(no Lease CAS past deadline), `test_wait_clamps_sleep_to_remaining_budget`.

## API / IaC diff summary

New modules:

- `api/services/blast/coordination.py` — pure config/invariants
  (`coordination_backend`, `is_k8s_backend`, `max_run_concurrency`,
  `submit_lease_ttl_seconds`, wait caps, grace/skew, `assert_coordination_invariants`).
- `api/services/k8s/submit_lease.py` — `k8s_acquire_submit_lease` /
  `k8s_release_submit_lease` (CAS, conditional, best-effort release that never
  raises).
- `api/services/blast/k8s_gate.py` — `acquire_k8s_admission` /
  `release_k8s_admission` / `wait_for_k8s_admission` (combines Gate A + Gate B).

Modified:

- `api/services/k8s/blast_status.py` — `k8s_count_active_blast_submissions`
  (fresh uncached, fail-closed, phantom-slot liveness-bounded by
  `finalizer_grace_seconds`).
- `api/tasks/blast/submit_task.py` — k8s gate block + `finally` Lease release;
  carries **both** wait deadlines through requeues so an oscillation between
  submit_slot_busy and capacity_full cannot reset either bound.
- `api/tasks/blast/split_pipeline.py` — per-child inline gating, **Gate A only**
  (`check_capacity=False`) so a split cannot self-deadlock on its own shards.
- `api/celery_app.py` — `assert_coordination_invariants()` at import (no-op
  unless k8s backend).

No IaC change in this repo for Phase 0. No new env var is added to the Container
App template yet (flag is unset = legacy behaviour). The sibling repo
`dotnetpower/elastic-blast-azure` must pin the same `app=finalizer` selector +
`elb-job-id` dedup and have its on-AKS submit path acquire the same Lease — see
the cross-repo tracking issue (Charter §13).

## Validation evidence

- `uv run pytest -q api/tests/test_blast_coordination.py
  api/tests/test_blast_submit_lease.py api/tests/test_blast_k8s_gate.py
  api/tests/test_blast_gate_b_count.py` → green (incl. new fail-closed
  liveness, 401/403 forbidden, label-less finalizer count, unclamped invariant,
  and split gate-cap default cases).
- `uv run pytest -q api/tests/test_blast_submit_capacity_gate.py
  api/tests/test_blast_tasks.py` → green (incl. k8s §2a precedence,
  capacity-full requeue with jitter, submit-slot-busy requeue, lease-API-error
  retry, split-child gating, and parent-budget-exhausted fail-fast cases).
- `uv run pytest -q api/tests` → full suite green.
- `uv run ruff check api` → all checks passed.

All code paths are default-OFF; with `BLAST_COORD_BACKEND` unset the existing
suites remain green, confirming backward compatibility.
