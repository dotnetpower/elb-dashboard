# HTTP Inspector Modal

## Motivation

The Sidecars card rendered the HTTP request inspector inline below the topology, which pushed the dashboard layout down and made the graph/table interaction feel cramped.

## User-facing Change

The HTTP request inspector now opens in a modal dialog from the Sidecars card. The scatter plot keeps the existing point-click drawer, while table row clicks render a compact detail panel below the request list.

Scatter points are clamped away from the x/y axes by a small pixel margin, and the latency axis now derives its tick range from the visible request durations instead of using a fixed 5-3000 ms scale.

Selecting a request row scrolls the modal to the inline detail panel, and request/response body blocks wrap long lines so the full captured body remains visible without horizontal clipping.

Body blocks detect JSON, XML, and plain text. JSON/XML bodies are formatted when possible and rendered with lightweight syntax highlighting for keys, tags, values, numbers, and literals. Partial or truncated JSON still gets a best-effort structural layout.

Successful HTTP responses whose JSON body reports `degraded: true` or `external_degraded: true` stay HTTP 200 in the inspector but are visually marked with a `Degraded` warning badge in the chart, request table, tooltip, and detail panel. The Errors filter includes these semantic-degraded 2xx responses.

## API / IaC Diff Summary

- Frontend-only change.
- No API contract changes.
- No IaC changes.

## Validation Evidence

- `cd web && npm run build` passed.
- Browser check at `http://127.0.0.1:8090/`: Inspect HTTP requests opens as a modal dialog, the visible latency ticks follow the current sample range, and clicking a request table row renders the selected-request detail region below the table.
- Browser re-check: selecting a request row scrolls the modal to the selected-request detail region; request/response body blocks use wrapping instead of horizontal clipping.
- Browser re-check: a JSON response body is labelled as JSON in the selected-request detail panel; truncated JSON receives best-effort structural layout.
- Browser re-check: 200 responses with degraded JSON payloads show `Degraded` badges in the request table while preserving the HTTP status code; the window count chips include degraded requests separately.
- Browser re-check: degraded 2xx samples render as triangle markers using the `#e69b82` degraded tone, visually distinct from the circular 4xx and 5xx markers.
- Browser re-check: detail `curl` and response/request body `Copy` buttons switch to `Copied` after click, with fallback copy handling for browsers where `navigator.clipboard` is unavailable.
