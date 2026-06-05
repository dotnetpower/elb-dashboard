# 2026-06-06 — App Insights warning hygiene

## Motivation

A 24-hour App Insights sweep (`AppTraces SeverityLevel >= 2`) of the
moonchoi production `ca-elb-dashboard` revealed three log families that
were emitting WARNING-level rows on every poll tick instead of
collapsing to a one-line stale fallback:

| Family | Count (24h) | Root cause |
| --- | --- | --- |
| `sidecar metrics mget failed: Error 111 connecting to 127.0.0.1:6379` | 56 | Redis sidecar restart; `/api/monitor/sidecars` polls every few seconds and logged WARNING every tick |
| `ncbi rate-limit: redis EVAL failed (NoScriptError)` | 9 | redis-py 5.x strips the `NOSCRIPT` prefix from `NoScriptError.__str__` → the existing `"NOSCRIPT" in str(exc).upper()` check missed it, so EVALSHA→EVAL recovery never fired and every call degraded to the in-process bucket |
| `upgrade.history read failed: The range specified is invalid for the current size of the resource.` | 9 | Reading a 0-byte append blob (no upgrade events yet) returns HTTP 416 `InvalidRange`; `read_metadata_blob_bytes` re-raised instead of treating empty-but-existing as `b""` |

None of these are actually faults — they are repeated symptoms of
known-benign conditions (Redis sidecar restart, Lua script eviction,
fresh deployment with no upgrade history yet). Letting them flood
App Insights as WARNINGs / exception rows hides genuine new outage
classes.

Also identified for reference (no code change in this PR):

- `monitor snapshot refresh failed` / `k8s_warmup_status failed for
  elb-cluster-02` / `aks_top_nodes gracefully degraded` — all already
  go through the `_is_transient_refresh_failure` +
  `_should_suppress_transient_telemetry` 5-minute dedup window added
  earlier; they're behaving as designed during the AKS read-timeout
  bursts seen in the same window.
- `/api/upgrade/escape-hatch` 403 / 404 — expected response to an
  unauthorised caller or to a fresh deployment with no rollback
  snapshot recorded. Not a code defect.
- `/api/blast/taxonomy/detail/{taxid}` 503 — already returns the
  documented `taxonomy_lookup_unavailable` error with `retryable=true`
  and `retry_after_seconds=30`. Behaving as designed.

## User-facing change

None directly. Side effect: App Insights stops recording duplicate
WARNINGs / exception rows for the three families above, so dashboards
that alert on "WARNING spike" are no longer drowned out by benign
restart / first-deploy noise. The NCBI rate-limit bucket now actually
recovers from a Redis script eviction (previously every call after a
restart degraded to the in-process bucket and the cross-replica rate
ceiling was effectively lost).

## API / IaC diff summary

No public API change. Internal helper additions:

- `api/services/ncbi/_eutils.py` — new `_is_noscript_error(exc)`
  classifier (type-first, substring-fallback); `_consume_token_redis_until`
  uses it so EVAL fallback fires on `redis.exceptions.NoScriptError`.
- `api/services/peering_nsg_lock.py` — same classifier added; same
  EVALSHA → EVAL retry now matches `NoScriptError` instances.
- `api/services/storage/blob_io.py` — `read_metadata_blob_bytes` now
  catches `HttpResponseError` with `status_code == 416` or
  `error_code == "InvalidRange"` and returns `b""` (other 5xx /
  service errors still propagate).
- `api/services/sidecar_metrics.py` — new `_log_redis_unavailable(exc)`
  helper with a 300 s per-error-class dedup window; first failure
  logs WARNING, repeats inside the window degrade to DEBUG. Reset
  helper `_reset_redis_unavailable_dedup()` exposed for tests.

## Validation

- Lint: `uv run ruff check api/services/ncbi/_eutils.py api/services/peering_nsg_lock.py api/services/storage/blob_io.py api/services/sidecar_metrics.py` — clean.
- Focused tests (4 new + 1 existing): `uv run pytest -q api/tests/test_ncbi_nuccore.py::test_redis_token_bucket_recovers_from_noscript_eviction api/tests/test_peering_nsg_lock.py::test_redis_release_handles_redis_py_noscript_error api/tests/test_peering_nsg_lock.py::test_redis_release_falls_back_to_eval_on_noscript api/tests/test_blob_io_metadata.py api/tests/test_sidecar_metrics.py::test_collect_snapshot_dedups_redis_unavailable_warning` — 8 passed.
- Full backend suite: `uv run pytest -q api/tests` — **2932 passed, 3 skipped** in 61 s. No regressions.
- Repro confirmation (NoScriptError str): `python -c "from redis.exceptions import NoScriptError; print('NOSCRIPT' in str(NoScriptError('No matching script. Please use [E]VAL.')).upper())"` → `False`, confirming the silent-miss bug.

## Risk / follow-ups

- The dedup helper is per-process; a deployment with multiple `api`
  replicas would still emit one WARNING per replica per window. The
  current single-replica `minReplicas: 1, maxReplicas: 1` charter
  means there is exactly one emitter, so no aggregation needed.
- The `InvalidRange` collapse to empty intentionally only fires for
  status 416 / error_code `InvalidRange`. Other 4xx/5xx still raise
  so genuine failures (auth, throttling, network) remain visible.
- Will re-run the same KQL sweep 24 h after the next deploy to
  confirm the three families dropped to near-zero.
