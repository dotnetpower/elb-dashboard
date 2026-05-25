# Local log active-session guard

## Motivation

Long-running local services such as the Celery worker can keep writing logs while other local-run tasks start later. The local log cleanup kept only the newest session directories and could delete a still-active worker session, causing the log mirror to fail with `cannot redirect to worker.log.1` and terminating the wrapper with exit code 143.

## User-facing change

`scripts/dev/run-with-log.sh` now writes per-service active markers into the selected log session and skips cleanup for sessions whose marker PID is still alive. Stale markers are removed automatically during cleanup.

Local worker shutdown is also quieter: the log wrapper now sends graceful termination to the top-level command first and only escalates to the process group if it does not exit. The paired Celery workers run without gossip/mingle/heartbeat by default, removing repeated `missed heartbeat` noise between `worker-main` and `worker-artifacts` in local development.

## API/IaC diff summary

- Local development scripts and worker launcher only; no API or infrastructure changes.

## Validation evidence

- `bash -n scripts/dev/run-with-log.sh scripts/dev/local-run.sh`
- `uv run ruff check api/run_celery_workers.py`
- Restarted `scripts/dev/local-run.sh worker` and verified `.active.worker.<pid>` exists in the live log session.
- Verified Celery worker processes include `--without-gossip --without-mingle --without-heartbeat` and latest worker log has no `missed heartbeat`, `reentrant`, authentication, or `ResourceNotFoundError` entries.