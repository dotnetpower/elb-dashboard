# Sidecar HTTP Inspector — Backend wire-up + SidecarsCard integration

**Date:** 2026-05-16
**Author:** Copilot agent (continuation of `2026-05-16-sidecar-http-inspector.md`)

## Motivation

The 2026-05-16 mockup work landed three Variant proposals (timeline / scatter / virtualized table) and polished Variant A through three critique rounds. The mockups proved the UX, but Variant A was still wired to a static fixture array — operators could not see real captured traffic.

This change makes the inspector real:

* The `api` sidecar now captures every non-streaming HTTP request that flows through `RequestIdMiddleware` (request + response headers + body, redaction applied) into a process-local ring buffer.
* A new `/api/monitor/sidecar-requests` route exposes the buffer.
* The dashboard's `SidecarsCard` now has an **"Inspect HTTP requests"** toggle that lazily mounts the production-quality Variant A panel against the live endpoint.

## User-facing change

* **SidecarsCard, top-right toolbar:** new glass button "Inspect HTTP requests" (lucide `Activity` icon). Clicking expands a panel below the topology that shows the most recent 200 captured requests:
  * Latency vs. time scatter chart (last 5 min, log-scaled y-axis, 2 s SLA reference line)
  * Filter input (path / caller / request_id / status code)
  * Live table sorted newest-first (Time / Method / Path / Caller / Status / Duration / Size)
  * Click a row → glass drawer with full request/response headers, capped body, and a one-click `curl` reproduction string
* The panel polls the endpoint every 5 s and shows a `last refresh HH:MM:SS` indicator. Manual `Refresh` button is also present.
* Empty state is graceful ("No requests captured yet — buffer is per-process").
* Error state surfaces the failure inline without breaking the rest of the card.

## API / IaC diff summary

### Backend (`api/`)

| File | Change |
|------|--------|
| `api/services/request_metrics.py` | Added `_DetailSample` dataclass, `_DetailRingBuffer`, module-level constants (`DETAIL_CAPACITY_DEFAULT=256`, `DETAIL_BODY_CAP_BYTES=4 KiB`, `DETAIL_REDACT_HEADERS`, `DETAIL_CAPTURABLE_TYPES`), helpers `redact_headers()` / `is_capturable_content_type()` / `capture_body()` / `record_detail()` / `details()` / `reset_details_for_tests()`. The aggregate ring buffer (`record`) is unchanged. |
| `api/main.py` | `RequestIdMiddleware.dispatch` now: (1) skips capture for `/api/monitor/sidecars`, `/api/monitor/metrics`, `/api/monitor/sidecar-requests`, `/api/terminal/ws`, and `/api/health`; (2) for capturable requests, buffers the request body up to 64 KiB and replays via `request._receive`; (3) drains the response `body_iterator` in two passes (capture up to 4 KiB then drain remainder so the client still gets the full payload), rebuilds the `Response()` with the same headers minus `content-length`; (4) calls `record_detail(...)` after the existing `record(...)` aggregate call. Wrapped in `try/except` so an inspector failure never breaks a real request. New env switch `REQUEST_DETAIL_CAPTURE_ENABLED` (default `true`). |
| `api/routes/monitor.py` | Added `GET /api/monitor/sidecar-requests?limit=…` (1-1000, default 200). Returns `{items, count, capacity}`. Authenticated via the existing `require_caller` MSAL bearer dep. |
| `api/tests/test_request_metrics_detail.py` | **NEW** — 9 tests covering header redaction, content-type capturable detection, body cap behaviour (truncation marker), ring-buffer eviction, full middleware integration (POST request body captured + replayed to handler, response body captured + delivered intact), end-to-end through the `/api/monitor/sidecar-requests` route. Uses `monkeypatch.setenv()` for env isolation. |

**Redaction list (case-insensitive header names):** `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`, `x-auth-token`, `x-functions-key`, `x-ms-client-secret`. Replacement text: `********** (redacted)`.

**Capturable content types:** `application/json`, `application/x-www-form-urlencoded`, `text/*`. Binary bodies are replaced with `<binary N bytes — not captured>`.

### Frontend (`web/`)

| File | Change |
|------|--------|
| `web/src/api/monitoring.ts` | Added `monitoringApi.sidecarRequests(limit=200)` plus interfaces `SidecarRequestHeader`, `SidecarRequestSample`, `SidecarRequestsResponse`. |
| `web/src/pages/mockups/SidecarInspectorMockups.tsx` | Loosened `MockReq.method` to `string` union, added `export type InspectorRequest = MockReq;`, exported `VariantA`, replaced static `NOW` references with `Date.now()` (in `DetailContent.fmtAgo`) and a `referenceTs` derived from `max(data.ts)` (in `VariantA`'s window selection + `ScatterChart` `windowEnd`). The mockup now feeds itself from any `MockReq[]` array and is therefore reusable as a production component. |
| `web/src/components/cards/SidecarsCard/HttpInspectorPanel.tsx` | **NEW** — fetches `/api/monitor/sidecar-requests` every 5 s, maps `SidecarRequestSample` → `InspectorRequest` (multiplies backend epoch-seconds `ts` by 1000), renders `<VariantA data={mapped} />`. Provides loading/error/empty states, a manual refresh button, and a "X captured · capacity Y" subtitle. |
| `web/src/components/cards/SidecarsCard/SidecarsCard.tsx` | Added a glass "Inspect HTTP requests" toggle button to `rightSlot` (lucide `Activity` icon, ARIA `aria-expanded`/`aria-pressed`/`aria-controls`). When enabled, mounts `<HttpInspectorPanel />` inside a divider region below the legend (id `sidecar-http-inspector-panel`). |

### Infra
No infra change — the inspector is a process-local ring buffer in the same `api` sidecar that already runs.

## Validation evidence

### Tests
* `uv run pytest -q api/tests` → **420 passed**
* `uv run pytest -q api/tests/test_request_metrics_detail.py` → **9 passed**
* `uv run ruff check api/services/request_metrics.py api/tests/test_request_metrics_detail.py` → **All checks passed!**
* `cd web && npm run build` → built in 10.94s, no TS errors

### End-to-end smoke (live containers, `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml`)
1. Restart api + frontend with new images.
2. `curl -X POST -H 'Content-Type: application/json' -d '{"sample":"data","n":42}' http://127.0.0.1:18080/api/resources/_smoke` → 404 (route doesn't exist, expected).
3. `curl http://127.0.0.1:18080/api/monitor/sidecar-requests?limit=5` returns:
   ```json
   {"items":[{"ts":1778937636.4,"request_id":"cd39dbf7477a8bab","method":"POST","path":"/api/resources/_smoke","status":404,"duration_ms":1.29,"caller":null,"client_ip":"172.20.0.1","request_headers":[…],"request_body":"{\"sample\":\"data\",\"n\":42}","request_body_truncated":false,"response_headers":[…],"response_body":"{\"detail\":\"unknown api route\",\"path\":\"/api/resources/_smoke\"}","response_body_truncated":false,"response_size_bytes":61}],"count":1,"capacity":256}
   ```
   — request body, response body, and `request_id` are all captured intact; bearer headers (none in this curl) would have been redacted.

### Browser screenshots
* `docs/images/2026-05-16-sidecar-http-inspector-live.png` — SidecarsCard with inspector expanded, 33 captured requests, scatter chart, table with anonymous caller (auth-bypass dev mode).
* `docs/images/2026-05-16-sidecar-http-inspector-drawer.png` — drawer open on a `GET /api/blast/jobs` row showing Time/Caller/Client IP/Status/Duration, Request headers (host, sec-ch-ua, x-client-request-id, etc.), and Response section.

## Operational notes

* **Memory budget:** 256 entries × ~10-30 KiB ≈ ~5-8 MiB per `api` sidecar process. Acceptable for a single-replica Container App.
* **Privacy:** All bearer/cookie/api-key headers are redacted at capture time. Even if an operator copies the cURL reproduction string from the drawer, the auth header will read `********** (redacted)`. Subscription IDs / SAS tokens are *not* yet redacted server-side — the existing `services/sanitise.py` redactor will be plugged into the panel's display layer in a follow-up if real customer traffic surfaces such patterns.
* **Streaming endpoints excluded by design:** `/api/terminal/ws` (WebSocket), `/api/monitor/metrics` (SSE), `/api/monitor/sidecars` (high-frequency polling card itself — would be self-pollution), `/api/monitor/sidecar-requests` (the inspector reading itself).
* **Disable switch:** set `REQUEST_DETAIL_CAPTURE_ENABLED=false` in the api sidecar to stop capture without rebuilding (aggregate metrics keep working).
* **Lazy mount:** the panel does not poll until the operator clicks the button — zero cost on the dashboard's default render path.

## Follow-ups (out of scope)

* Pipe captured samples into the existing `services/sanitise.py` redactor for SAS / sub-id / GUID secondary scrubbing.
* Per-tenant role gating on the `/api/monitor/sidecar-requests` route once RBAC roles ship.
* Optional persistence (append-blob in Storage) so captures survive a sidecar restart — only if operators ask.
