# Dashboard polling bottleneck removal

Date: 2026-05-22

## Motivation

The dashboard's auto-refresh loop fans out 15–20 parallel queries per tick
(every 30 s by default, configurable down to 5 s). Profiling the request
path surfaced three avoidable hot spots that did not need any change in
UX, API contracts, or polling cadence to fix:

1. `RequestIdMiddleware` was draining the entire response body iterator
   into memory and rebuilding the response for every `/api/*` request so
   the per-request HTTP inspector panel could capture the payload. The
   high-volume monitor / blast polling GETs (AKS, storage, ACR, jobs,
   databases) paid this buffering cost on every tick even though their
   inspector value is low — the dashboard refetches them constantly and
   the same payload dominates the ring buffer, pushing one-shot calls
   (POST submit, DELETE) out.
2. `k8s_warmup_status` made up to 18 sequential Kubernetes API calls per
   invocation (6 top-level reads + node list + pod list + up to 12 pod
   log tails), called once every 60 s per cluster from
   `useClusterDbChips`. All six top-level reads are independent of each
   other; the per-pod log fetches are likewise independent.
3. `_get_k8s_session` built a brand-new `requests.Session` on every call,
   wrote the CA / client cert / client key to fresh `NamedTemporaryFile`
   handles, then unlinked them when the caller closed the session. The
   dashboard hits 7+ K8s helpers per tick, so this was 7+ session
   constructions + ~14 temp-file writes/deletes per tick per cluster
   with zero connection pool reuse across calls.

None of these required changing polling intervals, API contracts, or the
inspector behaviour for the calls users actually care about.

## User-facing change

- Faster dashboard refresh ticks for AKS / storage / ACR / jobs cards
  (response body is no longer materialised in the middleware for those
  GETs; HTTP keep-alive is reused across K8s reads).
- `k8s_warmup_status` wall time goes from "sum of 18 serial GETs" to
  "max of one parallel fan-out" — visibly snappier warmup chip strip on
  clusters with many warmup pods.
- HTTP inspector panel still captures every POST/PUT/PATCH/DELETE and
  every non-polling GET. The polling GETs listed in
  `_INSPECTOR_EXCLUDE_GET_PREFIXES` are simply not buffered.

No UI changes, no API schema changes, no polling cadence changes, no new
configuration required.

## API / IaC diff summary

- `api/main.py`
  - New `_INSPECTOR_EXCLUDE_GET_PREFIXES` tuple covering high-volume
    polling GETs (`/api/monitor/aks`, `/api/monitor/storage`,
    `/api/monitor/acr`, `/api/monitor/terminal`, `/api/monitor/cluster`,
    `/api/monitor/jobs`, `/api/blast/jobs`, `/api/blast/databases`,
    `/api/warmup`, `/api/me`).
  - `_inspector_should_capture(path, method="POST")` — method-aware
    overload. Old single-arg call sites keep working (default treats
    them as non-GET).
  - `RequestIdMiddleware` passes the request method through so the
    polling GET exclusion can fire.
- `api/services/k8s_monitoring.py`
  - `k8s_warmup_status` fans out six independent reads via
    `ThreadPoolExecutor(max_workers=6)`. Phase-2 dependents
    (`_mark_stale_warmup_nodes`, `_warmup_pods_and_logs`) also run in
    parallel once the warmup-jobs response is in.
  - `_warmup_pods_and_logs` parallelises up to 12 pod-log fetches with
    `ThreadPoolExecutor(max_workers=min(12, len(pod_names)))`.
- `api/services/k8s/client.py`
  - New `_K8sSessionEntry` + `_K8S_SESSION_POOL` keyed by
    `(subscription_id, resource_group, cluster_name, admin)` with a
    300 s TTL (override via `K8S_SESSION_POOL_TTL_SECONDS`).
  - Per-entry TTL is clamped by **both** the kubeconfig material's own
    `expires_at` and (for Bearer-auth sessions) the AAD token's
    `expires_on` minus a 60 s safety margin, so a pooled session never
    outlives its underlying credentials.
  - Pool size capped at `_K8S_SESSION_POOL_MAX_ENTRIES = 32`; when the
    cap is exceeded the entry closest to expiry is evicted first.
  - When the effective TTL collapses to non-positive (e.g. the AAD
    token is about to expire), `_get_k8s_session` hands out a
    one-shot non-pooled session whose `close()` does a real teardown +
    temp-file unlink — preserving the historical contract for callers
    that use `try: ... finally: session.close()`.
  - `_get_k8s_session` returns a pooled session on hits; `session.close()`
    is overridden to a no-op for pooled sessions so existing
    `try: ... finally: session.close()` call sites release back to the
    pool instead of tearing down the connection pool + temp files.
  - `reset_k8s_session_pool()` test helper + `atexit` drain so
    interpreter exit unlinks temp files. `_retire_entry` reuses
    `requests.Session.close` directly to bypass the no-op override.
- `api/services/k8s_monitoring.py`
  - Re-exports `reset_k8s_session_pool` for symmetry with
    `reset_k8s_credential_cache`.
- `api/conftest.py`
  - Autouse fixture now also calls `reset_k8s_credential_cache()` +
    `reset_k8s_session_pool()` before and after each test so the pool
    cannot leak state across tests.

## Validation

- `uv run pytest -q api/tests` — **1067 passed in 32.69 s** (was 1022 baseline;
  +45 new focused regression tests across two hardening rounds).
- `uv run ruff check api` — clean.
- New focused tests:
  - `api/tests/test_inspector_exclude.py` — method-aware exclusion,
    backward-compat single-arg call sites, polling vs non-polling GETs.
  - `api/tests/test_k8s_session_pool.py` — pool reuse, key isolation
    (admin / cluster), TTL clamp by material expiry, TTL clamp by AAD
    token expiry with safety margin, throwaway path actually unlinks
    temp files, max-entries eviction picks the soonest-expiring entry,
    pooled `close()` is a no-op until `reset_k8s_session_pool()` retires
    the entry, **eviction never holds the pool lock during retire IO,
    throwaway close() is idempotent, `K8S_SESSION_POOL_MAX_ENTRIES`
    env override clamps into [1, 4096]**.
  - `api/tests/test_k8s_warmup_status_parallel.py` — every expected
    Kubernetes URL is issued exactly once across the parallel fan-out,
    non-200 responses degrade to empty defaults instead of raising,
    pod-log fan-out finishes in well under serial time.

## Critical-review hardening (round 2)

After the initial implementation a second critical-review pass turned up
four real risks; each was fixed and locked in by a regression test:

- **Lock held during IO**: `_get_k8s_session`'s slow path used to call
  `_retire_entry(...)` (TCP teardown + temp-file unlink) while holding
  `_K8S_SESSION_POOL_LOCK`. Every other `_get_k8s_session` caller across
  every cluster blocked on that IO. Fixed by collecting victims under
  the lock and retiring them after release. Covered by
  `test_pool_lock_released_during_retire_io`.
- **`atexit` deadlock risk**: `_atexit_drain_pool` used a blocking
  acquire; daemon threads forcibly terminated during interpreter
  shutdown could hold the lock and deadlock the atexit chain. Now uses
  `lock.acquire(blocking=False)` and silently skips on contention.
- **Hardcoded pool cap**: `_K8S_SESSION_POOL_MAX_ENTRIES = 32` was not
  configurable, inconsistent with the TTL helpers. Now routed through
  `_k8s_session_pool_max_entries()` which honours
  `K8S_SESSION_POOL_MAX_ENTRIES` clamped into `[1, 4096]`. Covered by
  `test_max_entries_env_override`.
- **`reset_k8s_session_pool` swallows sibling failures**: the retire
  loop now isolates per-entry exceptions so one bad entry cannot strand
  the rest.
- **`_inspector_should_capture(method=None)`**: defensive normalisation
  so a caller forwarding an unset header cannot crash the middleware
  with `AttributeError: 'NoneType' object has no attribute 'upper'`.

No frontend or infra changes — Tier 1 + Tier 2a validation only, no
redeploy required per
[.github/copilot-instructions.md §13](../../../.github/copilot-instructions.md).
