# Local API Port Guard

## Motivation

Starting the host-mode API while another uvicorn reloader was already bound to
`127.0.0.1:8085` produced only `ERROR: [Errno 98] Address already in use` in
`.logs/local/latest/api.log`. That made it hard to distinguish a healthy
already-running API from a real port conflict.

## User-Facing Change

`scripts/dev/local-run.sh api` now keeps the API on the existing local port but
adds a per-port startup lock and a health preflight. If `/api/health` is already
healthy, the command exits successfully and tells the developer to stop the
existing process only when they need a fresh reloader. If another listener owns
the port, the log prints the process details from `ss` or `lsof`.

## API/IaC Diff Summary

- No runtime API or IaC change.
- Local development helper only: `scripts/dev/local-run.sh` now wraps API
  startup in `run_api()` with lock, health, and listener diagnostics.
- `scripts/dev/README.md` documents the local API port-guard behavior.

## Validation Evidence

- `bash -n scripts/dev/local-run.sh`
- `python3 -m http.server 8085 --bind 127.0.0.1` plus
  `scripts/dev/local-run.sh api` reports the non-API listener instead of the raw
  uvicorn bind error.
- `scripts/dev/local-run.sh api` plus a second `scripts/dev/local-run.sh api`
  reports the already-running healthy API and exits successfully.