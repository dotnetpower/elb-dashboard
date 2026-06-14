---
title: Fix terminal WebSocket / SSE 403 — pin the api sidecar to a single uvicorn worker
description: The api sidecar ran uvicorn with --workers 2, which split the process-local one-shot ticket stores so every browser terminal WebSocket and SSE log/metric upgrade 403'd or degraded to polling. Reverted to a single worker.
tags:
  - terminal
  - operate
---

# Fix terminal WebSocket / SSE 403 — single uvicorn worker

## Motivation

Live verification of the deployed control plane (v0.2.359, revision
`ca-elb-dashboard--0000432`) found the **browser terminal never connects**: the
`wss://…/api/terminal/ws?ticket=…` upgrade returns HTTP 403 on every attempt and
the page loops `Disconnected (code 1006); reconnecting…`. The terminal sidecar
itself was healthy (`127.0.0.1:7681`, last probe HTTP 200), so the failure was in
the api sidecar's ticket validation, not ttyd.

## Root cause

The api `Dockerfile` started uvicorn with `--workers 2` (introduced in commit
`71ac408`). The api sidecar holds several **process-local, one-shot ticket
stores** that are written by one HTTP request and redeemed by a *second*
connection:

| Store | Module | Surface |
|-------|--------|---------|
| `_tickets` | [api/routes/terminal/ws.py](../../../api/routes/terminal/ws.py) | browser terminal WebSocket |
| `_tickets` | [api/routes/blast/logs.py](../../../api/routes/blast/logs.py) | BLAST live-log SSE |
| `_sidecar_tickets` | [api/routes/monitor/sidecars.py](../../../api/routes/monitor/sidecars.py) | sidecar metrics SSE |
| `_log_tickets` | [api/routes/monitor/logs.py](../../../api/routes/monitor/logs.py) | sidecar log SSE |

With two worker processes, `POST /api/.../ticket` is stored in worker A's
in-memory dict while the follow-up WebSocket / `EventSource` upgrade is routed
to worker B, whose dict has no such ticket → `4401` → the browser sees a 403 on
the handshake.

Blast radius:

* **Terminal: hard broken.** A WebSocket has no fallback transport, so the shell
  never opens.
* **SSE log/metric streams: degraded.** They fall back to 30 s polling instead of
  the advertised 5 s live stream (graceful, but a regression).

The api container is sized at `0.5` vCPU, so the second worker provided no real
parallelism for this async, I/O-bound workload — it only broke the ticket flows.
The whole control plane is intentionally a single pinned replica with in-process
state (the redis broker is a sibling sidecar), which the multi-worker start
silently violated.

## User-facing change

The browser terminal connects again, and the sidecar metric/log and BLAST
live-log streams return to real-time SSE instead of polling.

## API / IaC diff summary

- [api/Dockerfile](../../../api/Dockerfile) — uvicorn `--workers 2` → `--workers 1`,
  with a comment documenting the process-local ticket-store coupling.
- [api/tests/test_dockerfile_single_worker.py](../../../api/tests/test_dockerfile_single_worker.py)
  — new guard: fails if the api Dockerfile (or a Bicep command override) ever
  starts more than one uvicorn worker again.

No Bicep / env changes. The api sidecar already relied on the Dockerfile CMD
(no `command`/`args` override in `containerAppControl.bicep`).

## Validation evidence

- `uv run pytest -q api/tests` → 3505 passed, 3 skipped (was 3503 + 2 new guard tests).
- `uv run ruff check api` → clean.
- Live, post-deploy: browser terminal opens a shell over `wss://…/api/terminal/ws`
  (no 403); sidecar runtime card reports live SSE rather than polling.
