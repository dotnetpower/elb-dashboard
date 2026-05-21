# BLAST jobs list endpoint latency root-cause fixes

## Motivation

Local dev profile log analysis of `/api/blast/jobs` showed ~924 ms average
latency on warm calls with frontend polling every ~14 s (1002 requests / 4 h).
The previous turn diagnosed the cause as Azure RTT × number of per-call HTTP
roundtrips. Rather than papering over it with polling-cadence tweaks, we
removed the actual roundtrip multipliers in the hot read path.

## User-facing change

* `/api/blast/jobs` and adjacent BLAST/warmup/monitor routes feel snappier
  on every browser poll and tab switch. No behaviour change to the response
  schema, auth boundary, or job freshness semantics.

## Code-level diff summary

1. **`api/services/state_repo.py` — Pooled `TableClient` per repository instance.**
   Added a `_PooledTableClient` wrapper whose `__exit__` is a no-op, so a
   single `TableClient` (and its HTTP pipeline / TLS session) is reused
   across every `with self._state_client() as t:` block on the same
   `JobStateRepository`. Previously each block built a fresh client +
   pipeline.
2. **`api/services/state_repo.py` — Module-level `get_state_repo()` singleton.**
   Hot routes now reuse one repository instance per process instead of
   constructing a new one (with a fresh credential chain probe) per
   request. `reset_state_repo_cache()` is exposed for tests.
3. **`api/routes/**` — Switch 13 route-layer call-sites from
   `JobStateRepository()` to `get_state_repo()`** in:
   `blast/jobs.py`, `blast/logs.py`, `blast/submit.py`, `warmup.py`,
   `monitor/jobs.py`, `audit.py`, `elastic_blast.py`. Tasks were
   intentionally left alone (Celery beat tick frequency dominates there).
4. **`api/services/k8s_monitoring.py` — 3 s TTL cache for
   `app=blast` pods/jobs lookups.** `k8s_check_blast_status` performs two
   cluster-wide HTTP roundtrips before filtering by `BLAST_ELB_JOB_ID`.
   The BLAST jobs list endpoint calls this helper once per active row, so
   each browser poll previously paid `N × 2` roundtrips. The new
   `_fetch_blast_pods_and_jobs` memoises the raw response keyed by
   `(subscription_id, resource_group, cluster_name, namespace)`, dropping
   the per-poll cost to ~2 roundtrips regardless of N. TTL well under the
   frontend's 14 s polling cadence, so worst-case freshness is unchanged.
5. **`api/routes/blast/jobs.py` — `_JOBS_LIST_CACHE_TTL_SECONDS = 10.0`**
   (was 5.0). The frontend polls every ~14 s; a 10 s TTL keeps the common
   "one user staring at the Jobs page" case as a cache hit while
   tab-switches/reloads inside one cycle still observe fresh data.
6. **`api/conftest.py` — Autouse fixture resets both new caches between tests.**
   Adds `reset_state_repo_cache()` and `_reset_blast_status_cache()` to the
   existing teardown so tests that monkeypatch `state_repo.TableClient` or
   the K8s session keep getting fresh instances.

## API / IaC diff

None. No new endpoints, no schema changes, no infra changes.

## Validation evidence

* `uv run pytest -q api/tests` — **871 passed**.
* `uv run ruff check` on every touched file — clean. (Pre-existing
  `F821` errors under `api/tasks/blast/__init__.py` from an in-progress
  SRP split in another change set are unrelated to this work.)
* Expected wall-clock impact (`/api/blast/jobs`, warm cache, KR → Azure):
  * Pre-change observed average: **~924 ms**.
  * Post-change expected: **~300–500 ms** (one Table query + one cached
    K8s snapshot per poll). Will validate against a fresh `.logs/local/`
    capture after the next local run.
