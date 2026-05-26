# Detached Local Server Controls

## Motivation

Natural-language prompts such as "start the server" or "restart the server"
were easy to translate into a foreground `local-run.sh api` / `web` / `worker`
command. Those commands are correct for individual attached terminals, but they
keep the invoking shell open for long-running development services.

## User-Facing Change

- `scripts/dev/local-run.sh start` launches Redis, terminal-exec, api, worker,
  beat, and web in detached background processes and returns immediately.
- `scripts/dev/local-run.sh restart` stops the local ELB development services,
  then performs the same detached start.
- `scripts/dev/local-run.sh stop` and `scripts/dev/local-run.sh status` provide
  the paired shutdown and inspection commands.
- `scripts/dev/local-run.sh start` performs the shared local Azure CLI context
  check before detaching service processes, so a wrong subscription fails in the
  foreground instead of being buried in per-service logs.
- [VS Code Tasks](https://code.visualstudio.com/docs/debugtest/tasks) now expose
  `server: start`, `server: restart`, `server: stop`, and `server: status`; the
  existing `fullstack: start` task delegates to the detached launcher.

## API / IaC Diff

No deployed API or IaC change. This only changes local development scripts,
workspace tasks, and contributor documentation.

## Validation Evidence

- `bash -n scripts/dev/local-run.sh`
- `python3 -m json.tool .vscode/tasks.json >/dev/null`
- `git --no-pager diff --check -- scripts/dev/local-run.sh .vscode/tasks.json AGENTS.md scripts/dev/README.md docs/features_change/2026-05/2026-05-26-detached-local-server-controls.md`
- `scripts/dev/local-run.sh stop && scripts/dev/local-run.sh start && scripts/dev/local-run.sh status` after switching to the expected local Azure CLI subscription: status reported Redis, terminal-exec, api, worker, beat, and web as `running` while the `start` command had already returned.
- `scripts/dev/local-run.sh stop && scripts/dev/local-run.sh status`: status reported all services as `stopped`; `ss` showed no listeners on 8085, 8090, 6379, 7682, 18080, or 10000-10002.