# Local Server Log Guarantee

## Motivation

Local server starts could exit before entering `run-with-log.sh` when a service was already running, a port was occupied, or an environment preflight failed. That left VS Code tasks and direct `scripts/dev/local-run.sh api` launches without an `api.log` for the exact command the developer had just run.

## User-Facing Change

Long-running local server commands now create their service log before any startup preflight runs. Direct terminal launches and VS Code tasks for `api`, `worker`, `beat`, `web`, and `terminal-exec` all write to `.logs/local/latest/<service>.log`, including healthy no-op exits such as "already running" and startup failures such as port conflicts. Local log retention now keeps the newest 20 sessions by default so recent server evidence is less likely to be pruned by nearby Redis or Compose helper commands.

## API/IaC Diff Summary

- No runtime API or IaC change.
- `scripts/dev/local-run.sh` now re-enters long-running server subcommands through `scripts/dev/run-with-log.sh` before environment checks, port checks, stale-process cleanup, or the final process exec.
- `scripts/dev/run-with-log.sh` raises the default `LOCAL_LOG_KEEP_SESSIONS` from 3 to 20.
- README and `scripts/dev/README.md` document the updated retention default.

## Validation Evidence

- `bash -n scripts/dev/local-run.sh scripts/dev/run-with-log.sh scripts/dev/compose-with-log.sh`
- `LOCAL_LOG_SESSION=log-guarantee-api-help LOCAL_LOG_CONSOLE=false ELB_SKIP_AZURE_CONTEXT_CHECK=1 scripts/dev/local-run.sh api -- --help` creates `.logs/local/log-guarantee-api-help/api.log` and records the uvicorn help output.
- `LOCAL_LOG_SESSION=log-guarantee-api-already-running LOCAL_LOG_CONSOLE=false ELB_SKIP_AZURE_CONTEXT_CHECK=1 scripts/dev/local-run.sh api` with a temporary `/api/health` server on `127.0.0.1:8085` exits successfully and records the "api already running" diagnostic in `.logs/local/log-guarantee-api-already-running/api.log`.
- `LOCAL_LOG_SESSION=log-guarantee-web-conflict LOCAL_LOG_CONSOLE=false ELB_SKIP_AZURE_CONTEXT_CHECK=1 WEB_PORT=8090 scripts/dev/local-run.sh web` with a temporary listener on `127.0.0.1:8090` creates `.logs/local/log-guarantee-web-conflict/web.log` and records the port-owner diagnostic.
- A real Azure CLI subscription mismatch during validation was also recorded in `.logs/local/log-guarantee-api-help/api.log`, confirming environment preflight failures now leave service logs.
