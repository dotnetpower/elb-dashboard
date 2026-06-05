---
title: Per-operator browser terminal session isolation
description: The browser terminal now gives each authenticated operator their own tmux session instead of sharing one global session, so one person never sees another person's shell, scrollback, or az login context.
tags:
  - terminal
  - security
---

# Per-operator browser terminal session isolation

## Motivation

The `terminal` sidecar started [ttyd](https://github.com/tsl0922/ttyd) with a
fixed `tmux new-session -A -s elb`. The `-A` flag attaches to the existing
session when one exists, so **every operator who opened the browser terminal
attached to the same `elb` tmux session** — the same PTY, the same scrollback,
and the same `az login` context. A user opening the terminal would see the
commands, device codes, and working directory of whoever used it before or
concurrently. This was tracked as issue #2 in
[docs/copilot/security-audit-followup.md](../../copilot/security-audit-followup.md).

## User-facing change

Each authenticated operator now gets — or, on first connect, re-attaches to —
their **own** tmux session. A browser refresh by the same operator re-attaches
their session (in-progress work is preserved, same as before); a different
operator never lands in someone else's shell.

## Implementation

- **`terminal/tmux-attach.sh`** (new, installed as `/usr/local/bin/elb-tmux-attach`):
  takes a session-name token as `$1`, sanitises it to `[a-z0-9]` (defence in
  depth — the value is only ever used as a tmux session name via argv, never
  shell-evaluated), and `exec`s `tmux new-session -A -s "elb-<token>"`. It also
  sets a **per-operator `AZURE_CONFIG_DIR=$HOME/.azure-<token>`** via tmux `-e`
  so each operator's `az login` token cache is isolated too (see "Credential
  isolation" below). Without an argument it falls back to `elb-shared` for
  manual `kubectl exec` / local compose use.
- **`terminal/entrypoint.sh`**: ttyd now runs with `-a` (`--url-arg`) and
  launches `elb-tmux-attach` instead of the fixed `tmux new-session -A -s elb`.
- **`api/routes/terminal/ws.py`**: new `_session_arg(owner_oid)` derives a
  stable, non-reversible token (`u` + first 16 hex of `sha256(object_id)`) and
  `_build_upstream_url(owner_oid)` builds the loopback ttyd URL as
  `…/ws?arg=<token>`. The WebSocket proxy calls `_build_upstream_url` — the
  **only** input is the server-side `owner_oid` from the validated ticket;
  nothing the browser sends reaches the URL (argv boundary). The connect log
  line now also records the derived `tmux_session=<token>` for incident
  correlation (the token is a non-reversible hash, safe to log).
- **`terminal/Dockerfile` + `terminal/Dockerfile.runtime`**: `COPY` and `chmod`
  the new wrapper.

## Credential isolation

Isolating the tmux PTY alone would have been a *false* fix: every interactive
shell still shares `$HOME` (`/home/azureuser`), so one operator's `az login`
token in `~/.azure` would be reused by another operator's (now PTY-isolated)
shell — they could run `az` / `azcopy` / `elastic-blast` as the first
operator's identity. The wrapper therefore points each session at its own
`AZURE_CONFIG_DIR=$HOME/.azure-<token>`. `azcopy` honours this too: `profile.sh`
sets `AZCOPY_AUTO_LOGIN_TYPE=AZCLI`, which shells out to `az`, and `az` reads
`AZURE_CONFIG_DIR`.

## Known follow-up (out of scope here)

A reaper to kill idle per-operator tmux sessions is not yet shipped. Today the
sessions are bounded only by Container App revision restarts (ephemeral `$HOME`,
`minReplicas=1`). Tracked as the original PR2 in
[docs/copilot/security-audit-followup.md](../../copilot/security-audit-followup.md)
issue #2.

## Validation

- `bash -n terminal/tmux-attach.sh terminal/entrypoint.sh` — shell syntax OK;
  sanitisation verified (`BAD;rm -rf/` → `elb-rmrf`, empty → `elb-shared`).
- `uv run pytest -q api/tests/test_terminal_entrypoint.py
  api/tests/test_terminal_session_arg.py api/tests/test_terminal_ws_origin.py
  api/tests/test_terminal_ws_close_metrics.py` — argv-boundary + credential-cache
  guards included.
- `uv run pytest -q api/tests/ -k terminal -m ''` — broad terminal sweep.
- `uv run ruff check api/routes/terminal/ws.py api/tests/test_terminal_session_arg.py
  api/tests/test_terminal_entrypoint.py` — clean.
- `uv run python scripts/docs/check_frontmatter.py` — docs frontmatter guard.

> **Deploy note:** this change is baked into the `terminal` sidecar image
> (`terminal/Dockerfile*` + `entrypoint.sh`), so it only takes effect after a
> terminal sidecar image rebuild (`scripts/dev/quick-deploy.sh` terminal /
> `postprovision.sh`). Local pytest validation does not require a rebuild.
