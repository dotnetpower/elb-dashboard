# API Reference Request No Refresh

## Motivation

Clicking `Send Request` in the API Reference could refresh the SPA instead of keeping the response in-place. Two paths contributed: API Reference buttons relied on implicit button defaults, and the OpenAPI proxy's `401` responses used the app-wide MSAL redirect behavior.

## User-facing change

API Reference action buttons now explicitly use non-submit button semantics, so trying an endpoint runs the request without refreshing the page.

If the OpenAPI proxy returns `401`, the API Reference now renders that HTTP response in-place instead of triggering the app-wide MSAL redirect flow. Other application API calls keep the existing re-authentication behavior.

## API / IaC / deployment diff

- No backend API contract changes.
- No IaC changes.
- Frontend-only update in the API Reference components and the low-level API client.

## Validation

- `npx eslint src/pages/apiReference/EndpointCard.tsx src/pages/apiReference/ApiHero.tsx src/pages/apiReference/ResponseViewer.tsx src/pages/apiReference/TagSection.tsx --max-warnings 0`
- `npx eslint src/api/client.ts src/hooks/useOpenApiExecutor.ts src/pages/apiReference/EndpointCard.tsx src/pages/apiReference/ApiHero.tsx src/pages/apiReference/ResponseViewer.tsx src/pages/apiReference/TagSection.tsx --max-warnings 0`
- `npm run build`
- Production deploy: `api-docs-no-refresh-final-20260519060942`, revision `ca-elb-control--0000079`.
- Browser verification on `/docs?verify=0000079`: clicking `Send Request` for `/v1/health` keeps the same URL, preserves a page-global marker, performs only the `/api/aks/openapi/proxy` request, and renders the `401` response inline.