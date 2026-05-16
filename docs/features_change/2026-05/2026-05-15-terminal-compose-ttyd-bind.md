# 2026-05-15 — Fix local compose browser terminal reachability

## Motivation

The browser terminal did not work in the full local Docker Compose stack even though the `terminal` service reported healthy. There were three root causes.

First, the local networking model was wrong. Production Container Apps sidecars share loopback, but Docker Compose services do not.

`ttyd` was always bound to `127.0.0.1:7681` inside the `terminal` container while the `api` container was configured to proxy to `http://terminal:7681`. From the `api` container that produced `Connection refused`. The exec server on `:7682` worked because compose already overrode `EXEC_HOST=0.0.0.0`, so the terminal sidecar healthcheck missed the broken interactive shell path.

Second, the terminal image created an `azureuser` account but ended with `USER 1000:1000`. On Ubuntu 24.04 that UID/GID belongs to the base image's `ubuntu` user, while `azureuser` was created as UID 1001. As a result the container ran as `ubuntu` with `HOME=/home/ubuntu`, while the intended working directory `/home/azureuser` was owned by `azureuser`. After the bind fix, WebSocket upgrade succeeded but ttyd closed immediately with `uv_write: ESRCH (no such process)` because the shell/pty process died at session startup.

Third, the React terminal client used text frames (`"0" + data`, `"1" + resizeJson`) from an incorrect reading of ttyd's protocol. The ttyd web client actually sends the first message as raw JSON bytes (`{columns, rows}`) and then sends binary frames where byte 0 is the command (`"0"` input, `"1"` resize). Sending the text-frame resize as the first message made ttyd close the session before any shell output was produced.

## User-facing change

In the full local compose stack, opening `/terminal` through `http://127.0.0.1:18080/` can now reach the `ttyd` browser shell through the api WebSocket proxy.

Production posture is unchanged: `ttyd` still binds to `127.0.0.1` by default. Only `scripts/dev/docker-compose.full.yml` sets `TTYD_HOST=0.0.0.0` because compose containers need service-network reachability.

The terminal container also now runs as the intended `azureuser` account with `HOME=/home/azureuser`, so shell startup, Azure CLI profile state, and kubeconfig paths agree with the documented browser-terminal contract.

The React terminal now speaks ttyd's binary framing protocol, so input and resize events are accepted by ttyd instead of closing the session.

The terminal page is also more defensive around degraded conditions: the ticket request has an 8 second abort, terminal dimensions are clamped before they reach ttyd, late WebSocket/timer callbacks are ignored after unmount, and input/resize/output framing is isolated in test-covered helpers.

The api WebSocket proxy now verifies the upstream ttyd socket before accepting the browser WebSocket. If ttyd is unavailable, the browser does not see a false connected state. Proxy forwarding also tears down the paired forwarding task as soon as either side closes, so tab closes or upstream disconnects do not leave a dangling half-session.

## API / IaC diff summary

- `terminal/entrypoint.sh` now reads `TTYD_HOST`, defaulting to `127.0.0.1`, and passes it to `ttyd -i`.
- `terminal/entrypoint.sh` now forces the operator home to `/home/azureuser` unless `TERMINAL_HOME` is explicitly set.
- `terminal/Dockerfile` sets `HOME`, `USER`, and `SHELL`, and switches to `USER azureuser:azureuser` instead of numeric `1000:1000`.
- `scripts/dev/docker-compose.full.yml` sets `TTYD_HOST=0.0.0.0` for the `terminal` service.
- The compose `terminal` healthcheck now probes both `http://127.0.0.1:7681/` and `http://127.0.0.1:7682/healthz`, so an interactive-shell outage is no longer hidden by a healthy exec server.
- `web/src/pages/RemoteTerminal.tsx` now sends ttyd's initial JSON bytes and command-prefixed binary frames for input and resize.
- `web/src/pages/remoteTerminalProtocol.ts` centralises ttyd frame encoding/decoding and clamps terminal size to a stable range.
- `web/src/pages/RemoteTerminal.tsx` now aborts slow ticket requests, avoids state updates after unmount, disposes the xterm input listener, clears timers, and closes the WebSocket during cleanup.
- `api/routes/terminal_ws.py` now connects to ttyd before accepting the browser WebSocket and cancels the opposite forwarding task when either side closes.

## Validation evidence

- Before the fix, from inside the `api` container:
  - `http://terminal:7681/` failed with `Connection refused`.
  - `http://terminal:7682/healthz` returned `200`.
- After the bind fix but before the user fix, direct WebSocket to `ws://terminal:7681/ws` negotiated subprotocol `tty` but timed out without shell output; terminal logs showed `uv_write: ESRCH (no such process)`. Runtime inspection showed `uid=1000(ubuntu)`, `HOME=/home/ubuntu`, and `/home/azureuser` owned by `azureuser`.
- After the user fix, direct WebSocket still closed when using the old text-frame protocol. Inspecting ttyd's served JS showed `onSocketOpen()` sends raw JSON bytes first, while `sendData()` and resize send `Uint8Array` frames prefixed with ASCII command bytes. Retesting with binary framing returned `DIRECT_READY` from the shell.
- `bash -n terminal/entrypoint.sh` passed.
- `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml config` passed.
- `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml up -d --build terminal api` rebuilt the terminal image and recreated the local terminal / api services.
- Runtime user validation inside the terminal container returned `uid=1001(azureuser)`, `HOME=/home/azureuser`, `SHELL=/bin/bash`, `USER=azureuser`.
- From inside the `api` container:
  - `http://terminal:7681/` returned `200` with ttyd HTML.
  - `http://terminal:7682/healthz` returned `200`.
  - `http://127.0.0.1:8080/api/terminal/health` returned `{ "status": "ok", "upstream_status": 200 }`.
- Ticketed WebSocket round-trip through `ws://127.0.0.1:18080/api/terminal/ws?ticket=...` negotiated subprotocol `tty` and returned shell output containing `ELB_TERMINAL_READY_...`.
- `cd web && npm run build` passed after the React protocol fix.
- `cd web && npm run test -- remoteTerminalProtocol.test.ts` passed with coverage for terminal-size clamping, initial-size encoding, command-prefixed frames, and output-frame decoding.
- `uv run ruff check api/routes/terminal_ws.py` passed after the upstream-before-accept and forwarding cleanup changes.
- `uv run pytest -q api/tests/test_smoke.py api/tests/test_terminal_exec.py` passed (`31 passed`).
- `uv run pytest -q api/tests` passed (`120 passed`).
- Runtime ticket edge checks through `http://127.0.0.1:18080` confirmed reused, invalid, and missing tickets are rejected with WebSocket HTTP 403.
- Runtime WebSocket round-trip through `ws://127.0.0.1:18080/api/terminal/ws?ticket=...` returned shell output containing `ELB_HARDEN_...` after the proxy cleanup fix.
