# Monitor Response Snapshot Cache

## Motivation

Dashboard polling was still waiting on slow Azure control-plane and Kubernetes reads after auth and credential-level caches were verified. Repeated polls for the same monitor resource should not block on the same ARM/K8s call every time.

## User-facing change

Read-only monitor endpoints now return a short-lived server-side snapshot for repeated requests. Responses keep their existing payload shape and include a top-level `cache` metadata object with the snapshot state, age, TTL, and refresh timestamp.

Cached endpoints:

- `/api/monitor/aks`
- `/api/monitor/aks/nodes`
- `/api/monitor/aks/pods`
- `/api/monitor/aks/top-nodes`
- `/api/monitor/aks/warmup-status`
- `/api/monitor/aks/events`
- `/api/monitor/storage`
- `/api/monitor/acr`

Live endpoints such as pod logs, service IP discovery, run-command, metrics, and sidecar streams are not cached.

## API / IaC diff summary

- Added `api.services.monitor_cache.cached_snapshot()` with fresh-hit, stale-while-refresh, stale-if-error, disabled, bounded-capacity, and reset-safe modes.
- Wrapped slow read-only monitor routes in `api/routes/monitor.py` with stable cache keys based on subscription/resource identifiers.
- Added unit coverage for cache behavior and a route-level smoke test proving `/api/monitor/aks` reuses a snapshot.
- No IaC changes.

## Validation evidence

- `uv run ruff check api/services/monitor_cache.py api/tests/test_monitor_cache.py`
- `uv run python -m py_compile api/routes/monitor.py`
- `uv run pytest -q api/tests/test_monitor_cache.py api/tests/test_smoke.py::test_monitor_aks_uses_snapshot_cache api/tests/test_k8s_list_events.py api/tests/test_local_to_blast_job.py api/tests/test_state_repo.py` -> 23 passed
