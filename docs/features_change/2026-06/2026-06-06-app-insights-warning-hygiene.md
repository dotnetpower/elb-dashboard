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

### Round 1 — direct fixes for the three observed families

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

### Round 2 — preventative hardening for the same defect class

User asked to reinforce more broadly so other latent versions of the
same problem do not surface later. Added:

- `api/services/log_dedup.py` — **new shared helper**
  `dedup_log_warning(logger, key, msg, *args, window_seconds=300,
  exc_info=False)`. TTL-window dedup with a bounded tracked-key map
  (cap 1024) so a pathological caller emitting unique keys cannot grow
  the dict without bound. `exc_info=True` is forwarded only on the
  first emission per window so App Insights gets at most one
  exception row per outage class per window.
- `api/routes/monitor/common.py` `_graceful(...)` — dedup keyed by
  `(op, classification)`. Covers ~20 `/api/monitor/*` route call
  sites in one place, so a sustained AKS / Storage / ACR degrade
  now emits one WARNING per (route, classification) per window
  instead of one per polling tick.
- `api/services/k8s/warmup_status.py` — `k8s_warmup_status failed`
  warning now keyed by `(cluster, exc class)`. Was logging once per
  monitor tick during AKS read-timeout bursts.
- `api/services/auto_warmup_reconcile.py` — `auto warmup node readiness
  lookup failed` warning now keyed by `(cluster, exc class)`. Was
  logging once per 120 s beat tick during a sustained outage.
- `api/services/storage/blob_io.py` — `read_blob_text` now also
  catches HTTP 416 / `InvalidRange` and returns `""` for consistency
  with `read_metadata_blob_bytes`. Affects BLAST FAILURE.txt /
  runtime-out / metadata stub reads that may legitimately be 0 bytes.

## Validation

- Lint: `uv run ruff check api/services/log_dedup.py api/services/storage/blob_io.py api/routes/monitor/common.py api/services/k8s/warmup_status.py api/services/auto_warmup_reconcile.py api/services/ncbi/_eutils.py api/services/peering_nsg_lock.py api/services/sidecar_metrics.py` — clean.
- Focused tests (15 new across rounds): `uv run pytest -q api/tests/test_log_dedup.py api/tests/test_monitor_graceful.py api/tests/test_blob_io_metadata.py api/tests/test_sidecar_metrics.py api/tests/test_peering_nsg_lock.py api/tests/test_ncbi_nuccore.py` — **119 passed**.
- Full backend suite: `uv run pytest -q api/tests` — **2939 passed, 3 skipped** in 36 s. No regressions.
- Repro confirmation (NoScriptError str): `python -c "from redis.exceptions import NoScriptError; print('NOSCRIPT' in str(NoScriptError('No matching script. Please use [E]VAL.')).upper())"` → `False`, confirming the silent-miss bug.

## Risk / follow-ups

- The dedup helper is per-process; a deployment with multiple `api`
  replicas would still emit one WARNING per replica per window. The
  current single-replica `minReplicas: 1, maxReplicas: 1` charter
  means there is exactly one emitter, so no aggregation needed. If
  the topology ever moves to `maxReplicas > 1`, consider folding the
  dedup map into Redis db 2 (the same ops Redis already used by
  `sidecar_metrics`).
- The `InvalidRange` collapse to empty intentionally only fires for
  status 416 / error_code `InvalidRange`. Other 4xx/5xx still raise
  so genuine failures (auth, throttling, network) remain visible.
- The existing dedup logic in `api/services/monitor_cache.py` and
  `api/services/sidecar_metrics.py` was left in place rather than
  refactored to use the new shared helper — both are well-tested
  and functionally equivalent. A follow-up may consolidate them
  into the shared helper if further dedup sites are added.
- Will re-run the same KQL sweep 24 h after the next deploy to
  confirm the listed families dropped to near-zero.
