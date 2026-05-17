# Terminal Screen Scroll

## Motivation

The terminal screen did not scroll with the mouse wheel because a custom xterm wheel handler intercepted native xterm viewport scrolling. The local browser terminal could also enter a reconnect loop because each ttyd connection launched `tmux new-session -A -D`, which detached the previous ttyd client and caused its close handler to reconnect again.

## User-facing change

The terminal screen now relies on native xterm scrollback behavior with a larger explicit scrollback buffer. The terminal sidecar no longer passes `-D` to tmux, and tmux mouse mode plus a larger history limit are configured for wheel scrolling inside the persistent session. The Cockpit and Manual side menus are mutually exclusive so only one can be visible at a time.

## API / IaC diff summary

No API or IaC changes. This updates the frontend terminal page and terminal sidecar tmux/ttyd wiring.

## Validation evidence

- `cd web && npx tsc --noEmit`
- `cd web && npm run build`
- `uv run pytest -q api/tests/test_terminal_entrypoint.py api/tests/test_terminal_toolchain.py`
- `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml up -d --build terminal`
- `git --no-pager diff --check -- web/src/theme/glass.css docs/features_change/2026-05/2026-05-16-terminal-panel-scroll.md`
- Runtime inspection confirmed ttyd starts `tmux new-session -A -s elb` without `-D`, `tmux show-options -g mouse` returns `mouse on`, and `history-limit` is `100000`.
- Browser inspection confirmed only one of Cockpit / Manual is visible at a time, the terminal stays `connected`, and mouse wheel enters tmux history scrolling.
