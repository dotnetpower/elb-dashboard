# Local Server Restart Hardening

## Motivation

Restarting local VS Code tasks could leave child server processes behind when the wrapper process was terminated. The next start then hit duplicate listeners or crashed with address-in-use errors, and background exceptions outside request handling were not always easy to correlate in logs.

## User-facing change

- `scripts/dev/run-with-log.sh` now starts wrapped commands in a signal-forwarded process group and terminates that group on `SIGTERM`, `SIGINT`, or `SIGHUP`, reducing orphaned `uvicorn`, `vite`, and `terminal/exec_server.py` processes after task restarts.
- `scripts/dev/local-run.sh web` and `terminal-exec` now mirror the API guard: if their expected local endpoint is already healthy, they exit cleanly instead of spawning a duplicate process or crashing on a busy port.
- The API sidecar now installs process-wide `sys`, `threading`, and `asyncio` exception log hooks, plus a generic FastAPI unhandled-exception JSON handler, so request-external crashes land in logs with context.

## API / IaC diff summary

- Local development scripts only for restart behavior.
- API process logging only; no route contract or infrastructure changes.

## Validation evidence

- `bash -n scripts/dev/run-with-log.sh scripts/dev/local-run.sh`
- `uv run ruff check --fix api/app/global_exception_logging.py api/main.py api/app/lifespan.py`
- `uv run ruff format api/app/global_exception_logging.py api/main.py api/app/lifespan.py`
- `uv run pytest -q api/tests/test_smoke.py api/tests/test_request_metrics_detail.py`
- Duplicate guard smoke checks while services were already listening:
  - `LOCAL_LOG_CONSOLE=false bash scripts/dev/local-run.sh api`
  - `LOCAL_LOG_CONSOLE=false bash scripts/dev/local-run.sh web`
  - `LOCAL_LOG_CONSOLE=false bash scripts/dev/local-run.sh terminal-exec`