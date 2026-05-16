# 2026-05-15 — Browser terminal login banner and shell safety guard

## Motivation

The browser terminal is intentionally powerful: it carries `az`, `kubectl`,
`azcopy`, and the ElasticBLAST CLI inside the Container App's private network.
That power is useful for research operations, but the interactive shell had no
pre-execution safety layer for common destructive commands. A mistyped
`rm -rf`, broad Azure delete, or cluster-level `kubectl delete` could cause
avoidable damage.

The terminal also had a plain MOTD that was written to container logs at
startup but was not reliably shown inside the browser login shell.

## User-facing change

Opening the browser terminal now shows a colourful Unicode pixel-banner draft:
a large block-glyph `>_` prompt mark on the left and an italic, fast-slanted
`ElasticBlast CLI` wordmark on the right, followed by short session/guard/trace
text. Non-colour environments fall back to the plain `/etc/motd` text.

The terminal page also distinguishes the browser caller from the container's
Unix shell account: the ticket response includes the signed-in caller display
name, shell user, and a short session id, and the UI prints that logical session
above the ttyd banner. The shell process still runs as `azureuser`; this is the
shared sidecar runtime account, not the Microsoft Entra user.

This is logical session attribution, not full per-user OS isolation. A true
per-user terminal would require a PTY broker or per-user ttyd/tmux process model
that can assign isolated `HOME`, process lifetime, and audit boundaries from the
validated MSAL caller. The current sidecar keeps the production topology simple:
one terminal sidecar, one Unix account, authenticated WebSocket tickets, and
caller/session metadata shown in the UI.

The banner no longer leads with a second-login instruction. It presents the
browser-authenticated terminal session first; CLI-level Azure token handling is
kept out of the splash so the first screen does not imply the user must log in
again after the web session is already authenticated.

Interactive bash sessions source `terminal/command_guard.sh`, which installs a
`DEBUG` trap with `extdebug` so selected destructive commands are blocked
before execution. The guard blocks common host shutdown, disk formatting,
recursive deletion of protected paths, raw `dd` writes to `/dev/*`, inline or
piped shell execution, Azure delete operations, cluster-level or bulk
`kubectl delete`, and attempts to disable the guard.

The programmatic `exec_server` allowlist is unchanged and remains the security
boundary for api / worker initiated shell tooling.

## API / IaC diff summary

- `terminal/command_guard.sh` adds the interactive shell guard and a small
  test helper used by pytest.
- `terminal/banner.sh` renders the compact xterm-colour CLI splash and falls
  back to `/etc/motd` when colour is disabled or stdout is not a terminal.
- `terminal/profile.sh` runs `elb-banner` once per interactive login shell,
  configures azcopy for Azure CLI auth, keeps `az login` user-driven, and
  sources the command guard.
- `terminal/motd` now contains the ElasticBLAST terminal banner.
- `terminal/Dockerfile` copies and enables the new guard script.
- `api/tests/test_terminal_banner.py` covers the plain fallback and forced
  colour xterm rendering path.
- `api/tests/test_terminal_command_guard.py` covers allowed benign deletion,
  blocked recursive home deletion, Azure delete blocking, cluster-level
  kubectl delete blocking, and guard-disable blocking.
- `api/routes/terminal_ws.py` now returns caller/session metadata from
  `POST /api/terminal/ticket` and logs terminal WebSocket session ownership
  using short caller hashes instead of raw user identifiers.
- `web/src/pages/RemoteTerminal.tsx` renders the signed-in caller, shell user,
  and session id in the terminal header and xterm preamble.
- `infra/modules/containerAppControl.bicep` sets `TERMINAL_SHELL_USER=azureuser`
  on the api sidecar so the same session display contract is explicit in Azure
  deployments, not only local compose. The same pass replaces hardcoded Storage
  DNS suffixes in touched Bicep modules with `environment().suffixes.storage`.
- `scripts/dev/docker-compose.full.yml` and `scripts/dev/local-run.sh` exclude
  `api/tests/*` from uvicorn reload watching so editing/running tests no longer
  drops active terminal WebSocket sessions in local dev.

## Validation evidence

- `bash -n terminal/banner.sh terminal/command_guard.sh terminal/profile.sh terminal/entrypoint.sh` passed.
- `uv run ruff check api/tests/test_terminal_banner.py api/tests/test_terminal_command_guard.py` passed.
- `uv run ruff check api/routes/terminal_ws.py api/tests/test_smoke.py api/tests/test_terminal_banner.py api/tests/test_terminal_command_guard.py` passed.
- `uv run pytest -q api/tests/test_terminal_banner.py api/tests/test_terminal_command_guard.py api/tests/test_terminal_exec.py api/tests/test_smoke.py` passed (`47 passed`).
- `cd web && npm run build` passed.
- `scripts/dev/local-run.sh compose-full -- up -d --build redis terminal frontend api` rebuilt the terminal image and started the local terminal/api/frontend path.
- `az bicep build --file infra/main.bicep` passed after replacing hardcoded Storage DNS suffixes with `environment().suffixes.storage` in the touched Bicep modules.
- `curl http://127.0.0.1:18080/api/terminal/health` returned `{ "status": "ok", "upstream_status": 200 }`.
- `POST http://127.0.0.1:18080/api/terminal/ticket` returned a ticket with `caller.display_name=dev-bypass@local`, `shell_user=azureuser`, and a short `session_id`.
- The recreated compose api command line includes `--reload-exclude api/tests/*`, preventing local test-file edits from forcing uvicorn reloads that close active terminal WebSockets.
- Ticketed WebSocket smoke through `ws://127.0.0.1:18080/api/terminal/ws?...` returned shell output containing the ANSI colour logo, trace text (`browser >>> api >>> ttyd >>> shell`), and blocked `az group delete --name SHOULD_NOT_RUN --yes` before execution, then continued to the next command.
- Browser check on `http://127.0.0.1:18080/terminal` reported `connected` with no visible error. The header and xterm preamble showed `Signed in: dev-bypass@local`, `Shell: azureuser`, and a session id, followed by the colourful Unicode pixel-banner draft with a large `>_` prompt mark and slanted `ElasticBlast CLI` wordmark.
- A 20-second browser stability check on `http://127.0.0.1:18080/terminal` stayed `connected` with no terminal error and retained the logical session display.