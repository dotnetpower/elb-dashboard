---
title: Auto-warmup follow-up hardening + second critique (25 new issues)
description: Implements the bounded readiness gate, partial-subset fallback, failure circuit breaker, reconcile overlap lock, and 6 more reliability fixes, plus a verified triage of 25 newly-found auto-warmup issues.
tags:
  - blast
  - operate
  - architecture
---

# Auto-warmup follow-up hardening + second critique (25 new issues)

## Motivation

Follows [the root-cause + first critique change](2026-06-07-auto-warmup-rootcause-and-critique.md).
That change fixed the two root causes (forced re-warm intent lost after start; the
15-minute in-flight lock with no release) and left 28 issues open. This change
implements the highest-value open follow-ups and adds a second, deeper critique
pass that surfaced 25 more issues — fixing the real high-value ones and triaging
the rest with a verification verdict.

## Implemented follow-ups (from the first critique's Open list)

| # | Issue | Fix |
|---|-------|-----|
| #3 / #5 | Unbounded readiness gate; one never-Ready node blocks warmup forever; no ready-subset fallback | `auto_warmup_ready_gate` takes `waited_seconds` + `grace_seconds` (`AUTOWARMUP_NODE_WAIT_GRACE_SECONDS`, default 900 s). Past the grace window it returns `phase="ready_partial"` and warms the Ready subset (`require_all_warmup_nodes=False`). Wait elapsed is tracked in Redis (`autowarmup_wait_elapsed_seconds` / `_clear`). |
| #4 | `expected_warmup_node_count` trusts `pref.num_nodes`; an oversized value makes the gate impossible to satisfy | Caps the configured count by the cluster's live pool count (`min(configured, live)` when `live > 0`). |
| #8 | A permanently-failing DB is force-released + re-enqueued every 120 s forever, flooding telemetry | Per-(cluster, db) circuit breaker in Redis (`autowarmup_circuit_state` / `_reset`): after `AUTOWARMUP_CIRCUIT_THRESHOLD` (5) consecutive `Failed` observations the circuit opens for `AUTOWARMUP_CIRCUIT_COOLDOWN_SECONDS` (1800 s); a healthy observation resets it. |
| #11 | A slow reconcile can run concurrently with the next beat tick, double-processing every preference | Redis overlap lock (`SET NX EX 110`) around the beat (full-list) path; the single-preference force path is never gated. |
| #12 | `start_aks` enqueued the one-shot reconcile to the `storage` queue (delayed behind BLAST submits) | Routed to the dedicated `reconcile` queue, same as the beat reconcile. |
| #18 | `job_id` used only `int(time.time())` — two same-second ticks could collide | Appends a `uuid4` suffix. |
| #19 | `mark_auto_warmup_ready_state` rewrote the row + bumped the ETag every tick even when nothing changed | Skips the write when `last_ready` is unchanged and there is no trigger / force-clear. |
| #20 | The NCBI `latest-dir` HTTP lookup sat on the beat-tick critical path once per preference | 5-minute process-local TTL cache (`AUTOWARMUP_LATEST_VERSION_TTL_SECONDS`). |
| #24 | The warmup outer `except` logged "warmup verification failed" for all failures | Now "warmup_database failed db=… : …". |
| — | The seeded JobState payload hard-coded `require_all_warmup_nodes=True` even for a partial warm | Threaded through `_seed_auto_warmup_job_state(require_all_warmup_nodes=…)`. |

## New real bugs fixed (this critique pass)

1. **[HIGH] A single-shard DB broadcast across N nodes was reported warm after
   only 1 node finished.** `build_warmup_job_plan` emits one Job *per node*
   (`len(plan.jobs)` == node count) for a single-shard "full DB" broadcast, but
   `warmup_database` waited on `expected_jobs=selected_shards` (== 1). A search
   landing on any of the other N-1 still-cold nodes would fail with "database not
   found". Fixed: the wait target is now `max(1, len(plan.jobs))`.
2. **[MEDIUM] `wait_for_warmup_jobs` returned "completed" immediately when
   `expected_jobs <= 0`** (`nodes_ready >= 0` is always true), falsely reporting
   the DB warm without any Job warming. Clamped to `max(1, …)`.
3. **[MEDIUM] Node selection fell back to system-pool nodes** when no
   blastpool/user nodes existed. A warmup Job pinned to a `CriticalAddonsOnly`-
   tainted system node stays `Pending` forever and times the whole warmup out.
   `_candidate_warmup_node_names` now returns `[]` for a system-only cluster so
   the caller defers with "no Ready warmup nodes" instead of placing doomed Jobs.

## Critique — 25 issues found this pass, with verification verdict

A read-only exploration of the warmup / reconcile / k8s path surfaced 25 candidate
issues. Each was verified against the actual code; the verdict column records
whether it is a real defect (and its disposition) or a misread.

| # | Area | Severity | Verdict |
|---|------|----------|---------|
| 1 | `_mark_stale_warmup_nodes` marks Stale on a momentary node-snapshot blip (no debounce) | Medium | **Open** — real; needs a grace/debounce timer (first-critique #9). |
| 2 | Redis down → `inflight_acquire` fail-opens → same DB enqueued every 120 s | High | **Open (mitigated)** — duplicate warmups hit deterministic Job names (`warm-<db>-<shard>`) so K8s dedupes; only extra task/JobState churn. |
| 3 | Celery `retry_backoff_max` vs 4 h `wait_for_warmup_jobs` "task lifetime" | High | **Not a bug** — `retry_backoff_max` is inter-retry backoff, not a task time limit; the wait loop runs to completion. |
| 4 | `ready_partial` could pass `expected_node_count=0` with empty `ready_nodes` | Critical (claimed) | **Not a bug** — the partial branch is guarded by `ready_node_count >= 1`. |
| 5 | Mixed source-versions → `Stale` treated like `Failed` by the circuit | High | **Not a bug** — the reconcile resets `warm_meta={}` for `Stale`, so `warm_state` becomes "" and it re-warms (never enters the `Failed` circuit). |
| 6 | Warmup runs sharding on an in-flight prepare-db when `copy_status` absent | High | **Open (low risk)** — only legacy DBs lack `copy_status`; new prepare-db writes it, and the `file_count==0` gate catches empty DBs. |
| 7 | `expected_jobs` stale when the ready node set shrinks mid-warmup | High | **Open (acceptable)** — the wait times out → warmup fails → retried; bounded gate + circuit handle persistent cases. |
| 8 | Forced release returns `partial` but the task continues | High | **Not a bug** — `warmup_database` already raises when `force_release_summary["status"] != "released"` (first-critique hardening). |
| 9 | Circuit reset races a Job completing between two beat ticks | High | **Open (benign)** — worst case is one extra reset; no incorrect convergence. |
| 10 | Node selection returns system-pool nodes | High | **Fixed** (real bug #3 above). |
| 11 | `wait_for_warmup_jobs` `expected_jobs=0` → instant completion | Medium | **Fixed** (real bug #2 above). |
| 12 | Inflight slot "stuck held" 8 min after a Redis outage | Medium | **Open (self-heals)** — TTL is the backstop; shortened to 8 min in the first change. |
| 13 | Wait-since 6 h TTL expiry resets the grace window | Medium | **Not a bug** — the partial fallback fires at the 15-min grace and clears the key long before the 6 h TTL. |
| 14 | DB-name label truncation collision (`[:63]` → "db") | Low | **Open (unrealistic)** — BLAST DB names are short (`nt`, `core_nt`, `16S_…`). |
| 15 | `k8s_ensure_job_manifests` cannot distinguish "existing today" vs "collides" | Low | **Open** — names are deterministic + generation-annotated; mixed state is handled by the stale-release sweep. |
| 16 | Broadcast assumes the node list doesn't shrink during Job creation | Medium | **Open (acceptable)** — same timeout→retry path as #7. |
| 17 | Adaptive 60 s backoff in `wait_for_warmup_jobs` can miss a fast completion | Low | **Open (cosmetic)** — only delays the terminal UI update by ≤60 s; the warmup still completes. |
| 18 | `database_status_from_warmup_jobs` leaves `status` unset for `total_jobs=0` | Low | **Not a bug** — an entry only exists when ≥1 Job has the db label; the `else` sets `status="Unknown"`. |
| 19 | Mixed-shard naming masks Stale vs update_required | Low | **Open (cosmetic)** — both paths re-warm correctly; only the reason label differs. |
| 20 | Latest-version cache returns a stale value when NCBI is down | Low | **Not a bug** — on failure the helper returns "" (the version check is then skipped, which warms regardless = safe). |
| 21 | Circuit windowed counter boundary not explicitly checked | Low | **Not a bug** — the counter only opens at `>= threshold`; a fresh window starts at 1. |
| 22 | SPA cannot distinguish partial-warm from waiting in the gate response | Low | **Open (additive)** — `phase` + `partial` already disambiguate for the backend; SPA wiring is a future enhancement. |
| 23 | `expected_warmup_node_count` returns configured when `live==0` | Low | **Not a bug** — `cluster_is_workload_ready` requires `node_count>0`, so the gate returns `cluster_not_ready` first. |
| 24 | A killed warmup task abandons its JobState row; a later tick re-enqueues | Medium | **Open** — needs a JobState reaper for `warmup` rows stuck non-terminal (first-critique #17). |
| 25 | `mark_ready` no-op read can be stale vs a concurrent user PUT | Low | **Open (CAS-mitigated)** — the write path uses optimistic concurrency; the read-only no-op cannot clobber. |

**Disposition: 3 real bugs fixed, 9 misreads/not-bugs verified, 13 Open** (real
but deferred — debounce, JobState reaper, prepare-db race, SPA wiring). The
deferred items are tracked for a future pass; none re-introduce the "never
converges" failure modes the root-cause + this change already closed.

## Validation

- `uv run pytest -q api/tests/test_auto_warmup.py` — 31 passed (12 new across both passes).
- `uv run pytest -q api/tests/test_warmup_jobs.py` — 28 passed (2 new).
- `uv run pytest -q api/tests` — 3070 passed, 3 skipped.
- `uv run ruff check api` — clean on all touched files.

## Not deployed

Code-only change (no sidecar layout / Bicep / terminal toolchain). Per charter
§13 this is validated by pytest, not a redeploy. Ship with `quick-deploy.sh api`
(moonchoi MSAL overrides) when the behaviour is wanted live.
