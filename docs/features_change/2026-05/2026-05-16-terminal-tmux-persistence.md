# Terminal Tmux Persistence

## Motivation

The browser terminal was intended to reconnect to a persistent tmux session, but the entrypoint launched `ttyd` directly against `/bin/bash --login`. When the local api sidecar reloaded or a browser WebSocket reconnected, ttyd opened a fresh login shell and printed the ElasticBLAST banner again, making the terminal look like it had reset.

## User-facing change

The terminal sidecar now runs ttyd against `tmux new-session -A -D -s elb /bin/bash --login`, so reconnects attach to the existing terminal session instead of creating a new shell every time. New browser attaches also detach stale tmux clients, preventing multiple clients from fighting over pane size.

The browser terminal reconnect loop is now generation-guarded: stale WebSocket events are ignored, duplicate reconnect timers are suppressed, old input handlers are disposed before a new connection opens, and reconnect status stays in the page header instead of being written into the terminal scrollback.

The full local compose stack now publishes the Vite dev server on `127.0.0.1:8081` and configures Vite HMR to use that port, while the primary app still loads through the api proxy on `127.0.0.1:18080`. This keeps Vite's HMR WebSocket from hitting the api proxy root and producing 403 noise while testing the terminal page.

## API/IaC diff summary

- Updated `terminal/entrypoint.sh` to use the persistent tmux session described by the existing terminal architecture docs, with `-D` to detach stale clients.
- Hardened `RemoteTerminal` reconnect handling to avoid stale WebSocket handlers, duplicate reconnect attempts, noisy local reconnect banners, and mixed redraw scrollback after reconnect.
- Published the full-compose frontend dev server on loopback port `8081` and added compose-scoped Vite HMR host/port variables; the primary app entry remains the api proxy on `18080`.
- Added a regression test that prevents the entrypoint from returning to direct bash launch.
- No API or IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_terminal_entrypoint.py api/tests/test_terminal_banner.py api/tests/test_terminal_history.py` passed: 10 tests.
- `cd web && npm run build` passed.
- `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml config --quiet` passed.
- `git --no-pager diff --check -- terminal/entrypoint.sh api/tests/test_terminal_entrypoint.py web/src/pages/RemoteTerminal.tsx web/vite.config.ts scripts/dev/docker-compose.full.yml docs/features_change/2026-05/2026-05-16-terminal-tmux-persistence.md` passed.
- Local compose rebuild passed: `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml up -d --build terminal`.
- Local compose frontend recreation passed: `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml up -d frontend`; `curl -sI http://127.0.0.1:8081/` returned `HTTP/1.1 200 OK`.
- `GET http://127.0.0.1:18080/api/terminal/health` returned `{"status":"ok","upstream_status":200}`.
- Process inspection confirmed ttyd now runs `/usr/bin/tmux new-session -A -D -s elb /bin/bash --login`.
- After two forced WebSocket attach/close cycles, process counts were `tmux_clients=0 bash_login_shells=1`; after the visible browser reconnected, counts were `tmux_clients=1 bash_login_shells=1`.
- Browser verification on `/terminal` showed `connected`, one `ElasticBlast CLI` banner, visible prompt, and tmux status line.