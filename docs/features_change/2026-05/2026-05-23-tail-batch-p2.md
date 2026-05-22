# Tail-batch P2 — lifecycle + concurrency + streaming proxy + lock-free emit

## Motivation
Four small but cumulative wins, batched into one commit so the surface
area is easy to review together:

* `frontend_proxy._client` was never closed on uvicorn shutdown.
* `monitor_cache` and `storage_usage_cache` spawned a brand-new
  `threading.Thread` for every background refresh — under SSE +
  multiple monitor routes that meant 10+ thread spawns per dashboard
  tick.
* `event_emitter._get_client` paid a lock acquire on every `emit()`
  even after the singleton was already cached.
* `aks/openapi proxy` buffered the entire upstream response with
  `upstream.content` before returning; large BLAST result files
  multiplied through the api sidecar's RAM under concurrent clients.

## User-facing change
None. Lower steady-state thread/RAM footprint; clean keep-alive
shutdown for uvicorn reloads.

## API / IaC diff
* `api/routes/frontend_proxy.py` adds `close_client()`, called from
  `api/main.py::_lifespan`'s finally block.
* `api/services/monitor_cache.py` and
  `api/services/storage_usage_cache.py` route every
  `_start_refresh_thread` submission through a shared
  `ThreadPoolExecutor` (default 8 / 4 workers, env-overridable). Pools
  are torn down via `atexit`. Fallback daemon-thread path keeps the
  contract during the shutdown window.
* `api/services/event_emitter.py::_get_client` adds a lock-free
  cached-singleton fast path; double-checked locking inside.
* `api/routes/aks/openapi.py::aks_openapi_proxy` switches to
  `client.build_request` + `await client.send(req, stream=True)` and
  returns a `StreamingResponse(_body_iter())` that closes the upstream
  response and the temporary AsyncClient in its `finally`.

## Validation
* `uv run pytest -q api/tests/test_security_audit_bundle.py
  api/tests/test_monitor_cache.py api/tests/test_storage_usage_cache.py
  api/tests/test_event_emitter.py api/tests/test_external_blast_api.py`
  — passes.
* `uv run ruff check` on all four modules + `api/main.py` — clean.
