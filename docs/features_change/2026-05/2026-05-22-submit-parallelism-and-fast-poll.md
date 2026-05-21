# Per-cluster submit lock + fast SPA polling on running BLAST jobs

## Motivation

Two issues were reported against the running BLAST job page (e.g.
`/blast/jobs/27977ffb-…?tab=run`):

1. The step timeline appeared to freeze for ~5 s after **Warmup Check**
   before jumping to **Submit Job**. The intermediate `configuring`
   (~3 s) and `staging_db` (0 ms when the warmed SSD is reused) steps
   were never visible, so it looked like a stall.
2. Concurrent BLAST submits were serialized even though the Celery
   `worker-main` pool has `concurrency=2`. A second `submit` task always
   waited for the first to finish.

## User-facing change

- The Results page now updates within ~1 s during the fast-moving early
  phases (`preparing → warming_up → configuring → staging_db →
  submitting`) instead of every 5 s. Skipped steps (e.g. `staging_db`
  when `skip_warmed_ssd_init=true`) are still recorded as
  `status="skipped"` and now render in the timeline before the pipeline
  moves on, eliminating the perceived stall after Warmup Check.
- Once the pipeline reaches the steady phases (`running`,
  `exporting_results`) the cadence relaxes to 3 s to keep ARM/Storage
  load reasonable.
- Two BLAST submits targeting **different** AKS clusters (or different
  namespaces on the same cluster) now run in parallel. Submits targeting
  the **same** `(cluster, namespace)` continue to serialize — that is
  the constraint `elastic-blast submit` actually has, because it writes
  ServiceAccount/Secret/PVC/Job objects into one namespace and shares a
  working directory on the terminal sidecar.

## Implementation summary

### Backend ([api/tasks/blast/__init__.py](../../../api/tasks/blast/__init__.py))

- Replaced the single global Redis lock key `elb:blast:elastic-blast-submit`
  with a per-cluster, per-namespace key built by a new helper:

  ```python
  BLAST_SUBMIT_LOCK_KEY_PREFIX = "elb:blast:elastic-blast-submit"

  def _submit_lock_key(cluster_name: str, namespace: str) -> str:
      cluster = (cluster_name or "_unknown").strip() or "_unknown"
      ns = (namespace or "default").strip() or "default"
      return f"{BLAST_SUBMIT_LOCK_KEY_PREFIX}:{cluster}:{ns}"
  ```

- `_acquire_submit_lock` / `_release_submit_lock` now take `lock_key`
  as a keyword argument; the `submit` task computes
  `_submit_lock_key(cluster_name, "default")` once per invocation and
  reuses it for acquire/release.
- The "another submit is in progress" `RuntimeError` message now
  identifies the contended cluster/namespace so the audit log and the
  `error_code=blast_submit_lock_busy` retry chain are easier to trace.

### Frontend ([web/src/pages/blastResults/useBlastResultsState.ts](../../../web/src/pages/blastResults/useBlastResultsState.ts))

- Added a `FAST_POLL_PHASES` set covering `preparing`, `warming_up`,
  `configuring`, `staging_db`, `submitting`, and the back-off phase
  `waiting_for_submit_slot`.
- `jobQuery.refetchInterval` returns `FAST_POLL_INTERVAL_MS` (1 s) for
  those phases and the no-data bootstrap case, and
  `STEADY_POLL_INTERVAL_MS` (3 s) for `running` / `exporting_results`.
  Terminal and failure phases still return `false`.

## API / IaC diff summary

- No HTTP route signatures changed.
- No Bicep / Container App template changes.
- Redis key namespace is the only operational-surface change. The old
  key `elb:blast:elastic-blast-submit` is no longer written; if a stale
  entry from a previous deploy lingers, it auto-expires after the
  existing 900 s TTL.

## Validation evidence

- Lint: `uv run ruff check api/tasks/blast/__init__.py` → "All checks passed!"
- Backend tests: `uv run pytest -q api/tests/test_blast_tasks.py` →
  `118 passed in 4.04s`.
- Frontend build: `npm run -s build` in `web/` → `✓ built in 7.94s`.
- Frontend tests: `npm test -- --run` in `web/` → `Test Files 26 passed (26)`
  / `Tests 224 passed (224)`, including
  `src/components/BlastStepTimeline/stepState.test.ts` which exercises
  the `staging_db` skip → `submitting` transition the SPA now renders.
