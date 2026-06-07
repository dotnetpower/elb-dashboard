---
title: Terminal sidecar pins ttyd to loopback in a deployed Container App
description: The terminal entrypoint now hard-fails when TTYD_HOST is non-loopback inside a deployed Container Apps revision, matching the exec_server guard, so the interactive writable shell can never be exposed to the VNet.
tags:
  - security
  - operate
---

# Terminal sidecar pins ttyd to loopback in a deployed Container App (2026-06-07)

## Motivation

Charter §9 and the AGENTS.md tripwire require: "`ttyd` in the `terminal`
sidecar binds to **127.0.0.1 only** … the terminal must never be reachable
directly from the internet/VNet." The `exec_server` enforces this with a
hard-fail guard (`terminal/exec_server.py`): in a deployed Container Apps
revision (`CONTAINER_APP_NAME` is always set by the platform) it refuses any
non-loopback `EXEC_HOST` and raises at startup.

`terminal/entrypoint.sh` had **no equivalent guard for ttyd** — it only
*defaulted* `TTYD_HOST` to `127.0.0.1` (`TTYD_HOST="${TTYD_HOST:-127.0.0.1}"`).
An operator (or a bad env injection) setting `TTYD_HOST=0.0.0.0` would silently
start the **interactive writable shell** (`ttyd -W`) bound to all interfaces,
exposing it to anything that can reach the pod IP — the entire Container Apps
Environment VNet. ttyd is strictly more dangerous than the exec_server (full
interactive PTY vs. an argv-allowlisted exec channel), yet it was the one
without the guard.

## User-facing change

None for correct deployments (the default and the Bicep template both use
loopback). A misconfigured `TTYD_HOST` in a deployed revision now fails fast at
sidecar startup with a clear message instead of silently exposing the shell.

## API / IaC diff summary

- `terminal/entrypoint.sh` — after resolving `TTYD_HOST`, when
  `CONTAINER_APP_NAME` is set, a `case` guard allows only
  `127.0.0.1` / `localhost` / `::1` and otherwise prints a refusal and
  `exit 1` before `ttyd` starts. Mirrors the `exec_server.py` guard exactly.
- No IaC change (the deployed template already passes loopback; this closes the
  override hole).

## Validation evidence

- `uv run pytest -q api/tests/test_terminal_toolchain.py -m subprocess` — 4 passed,
  including the new `test_entrypoint_pins_ttyd_to_loopback_in_a_container_app`
  (deployed + `0.0.0.0` → exit 1; deployed + `127.0.0.1` → starts).
- `bash -n terminal/entrypoint.sh` clean; manual run confirmed
  `CONTAINER_APP_NAME=ca-elb TTYD_HOST=0.0.0.0` is refused.
- `uv run ruff check api/tests/test_terminal_toolchain.py` — clean.
