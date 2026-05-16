# Sidecar Card Stale API State

## Motivation

The dashboard could keep showing the last healthy Control Plane Sidecars snapshot when the API sidecar was alive at the container level but no longer responding to HTTP requests.

## User-facing change

The Control Plane Sidecars card now marks an old snapshot as stale and renders the sidecar nodes as degraded instead of continuing to show stale healthy state.

## API / IaC diff summary

No API or IaC changes. The frontend sidecar metrics hook now tracks snapshot age and exposes stale state to the card.

## Validation evidence

- `curl http://127.0.0.1:18080/api/health` returned HTTP 200 after restarting the hung local API sidecar.
- Browser fetch to `/api/monitor/sidecars` returned HTTP 200 with all six sidecars present.
- `npm run build` in `web/` completed successfully.
