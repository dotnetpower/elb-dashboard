# Live Wall Log Stream

## Motivation

The Live Wall page showed sidecar health metrics, but every log tile stayed in polling mode with "no recent activity" because the frontend was calling `/api/monitor/logs/*` endpoints that were not implemented on the FastAPI backend.

## User-facing Change

Live Wall now has backend support for per-sidecar log tickets, recent log tails, and SSE log streaming. Local development sessions read the existing `.logs/local/latest/*.log` files produced by `scripts/dev/local-run.sh`, sanitize sensitive values, and stream the tail into the six Live Wall tiles. Empty sidecar logs still open a live SSE connection immediately, so tiles no longer sit in a misleading polling/connecting state while waiting for the first line.

## API/IaC Diff Summary

- Added `POST /api/monitor/logs/ticket` for short-lived EventSource tickets.
- Added `GET /api/monitor/logs/{container}/recent?tail=N` for bounded backfill.
- Added `GET /api/monitor/logs/{container}/events?ticket=...` for SSE log tails.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_sidecar_logs.py api/tests/test_inspector_exclude.py` — 23 passed.
- `uv run ruff check api/routes/monitor/logs.py api/services/sidecar_logs.py api/tests/test_sidecar_logs.py` — passed.
- `cd web && npm run build` — passed.
- Local API smoke: `/api/monitor/logs/ticket`, `/api/monitor/logs/api/recent`, and `/api/monitor/logs/api/events` returned sanitized log data.
- Browser check on `http://localhost:8090/monitor/live-wall`: all six tiles reported `live`; the `api` tile rendered local API log lines, and sidecars without local log files rendered `no recent activity` instead of polling/connecting.