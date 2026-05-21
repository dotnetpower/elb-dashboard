# Local API Without Uvicorn Reload

## Motivation

Host-mode local API restarts should use one stable uvicorn process during BLAST debugging. The reload watcher can add process churn and make server restarts harder to reason about while diagnosing control-plane latency.

## User-facing change

`scripts/dev/local-run.sh api` now starts uvicorn without `--reload`. Developers restart the API process explicitly when they need new Python code loaded.

## API/IaC diff summary

- Removed `--reload`, `--reload-dir`, and `--reload-exclude` from host-mode `local-run.sh api`.
- Updated the existing-port message to refer to a fresh API process instead of a uvicorn reloader.
- No production API, frontend, or IaC changes.

## Validation evidence

- `bash -n scripts/dev/local-run.sh`: passed.
- `FRONTEND_UPSTREAM=http://127.0.0.1:8090 scripts/dev/local-run.sh api`: started `uvicorn api.main:app --host 127.0.0.1 --port 8085`.
- `curl -fsS http://127.0.0.1:8085/api/health`: returned `{"status":"ok","version":"0.0.1","revision":"local"}`.
