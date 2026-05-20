# Local Sidecar Probes

## Motivation

The host-mode local development loop starts Vite and `terminal/exec_server.py` as plain host processes, not as the production `frontend` and `terminal` sidecars. The Sidecars dashboard card only looked for Redis reporter keys, so local `frontend` and `terminal` appeared Down even when their local equivalents were reachable.

## User-facing change

In local development, the Sidecars card now treats the Vite frontend and terminal exec server as active when their loopback health probes respond. This keeps the local dashboard aligned with what the developer actually started.

## API / IaC / deployment diff

- No IaC changes.
- `GET /api/monitor/sidecars` keeps using Redis reporter metrics for deployed and compose sidecars.
- When `CONTAINER_APP_REVISION` is `local`, missing `frontend` and `terminal` reporter entries can be filled from `LOCAL_FRONTEND_HEALTH_URL` and `LOCAL_TERMINAL_HEALTH_URL` probes.
- Existing reporter entries, including malformed or stale entries, are not overridden by the local fallback.

## Validation

- `uv run pytest -q api/tests/test_sidecar_metrics.py` - 13 passed.
- `uv run ruff check api/services/sidecar_metrics.py api/tests/test_sidecar_metrics.py` - passed.
- Local `/api/monitor/sidecars` snapshot shows `frontend` and `terminal` as `ok` with `source=local_probe`.