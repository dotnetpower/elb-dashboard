# HTTP inspector live window controls

## Motivation

The HTTP request inspector is fed by the live sidecar request buffer and refreshes continuously. The 1m / 5m / 15m selector made the panel feel like a historical query surface even though it is a live operational view.

## User-facing change

- Removed the 1m / 5m / 15m time-window selector from the HTTP request inspector header.
- Kept the chart on the existing fixed 5-minute live window so the graph and table remain stable while SSE/live refreshes arrive.

## API / IaC diff summary

- Frontend-only change in `web/src/pages/mockups/SidecarInspectorMockups.tsx`.
- No API or IaC changes.

## Validation evidence

- `cd web && npm run build` -> passed, existing Vite large chunk warning only.