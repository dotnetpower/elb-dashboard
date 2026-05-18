# HTTP Inspector Layering Fix

## Motivation

The HTTP request inspector table uses a sticky header. When a request detail drawer opened from the chart, the table header could render above the drawer and make the panel look broken.

## User-facing change

The request detail drawer now consistently appears above the sticky request table header.

## API / IaC diff summary

- No API or IaC changes.
- Adjusted the HTTP inspector table and drawer stacking order in the frontend.

## Validation evidence

- `cd web && npm run build` -> passed.
- `cd web && npx eslint src/pages/mockups/SidecarInspectorMockups.tsx --max-warnings 0` -> passed.
- Deployed frontend image `acrelbnm5virmqrdi5c.azurecr.io/elb-frontend:http-inspector-layering-20260518b`.
- Container App revision `ca-elb-control--0000058` -> Healthy, 100% traffic.
- Public `/api/health?ui=http-layering` -> 200, revision `ca-elb-control--0000058`.
