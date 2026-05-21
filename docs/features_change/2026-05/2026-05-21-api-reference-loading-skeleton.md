# 2026-05-21 — API Reference loading skeleton

## Motivation

The API Reference page previously showed spinner-and-text loading states while
discovering the AKS-hosted OpenAPI service or loading the OpenAPI document. That
made the page feel empty, especially when cluster discovery was slow.

## User-facing change

The `/docs` page now renders an animated skeleton that matches the final API
Reference layout: a navigation sidebar placeholder, endpoint rows, response
chips, and a try-it panel placeholder. The same skeleton is used for service
discovery and OpenAPI specification loading.

## API and UI diff summary

| Area                      | Change                                                                                  |
| ------------------------- | --------------------------------------------------------------------------------------- |
| Service discovery loading | Replaces spinner-only copy with the API Reference skeleton.                             |
| Spec loading              | Uses the same endpoint-shaped skeleton while `openapi.json` loads.                      |
| Mobile layout             | Collapses the skeleton to one column so it follows the responsive API Reference layout. |

## Validation evidence

- `cd web && npm run lint -- --quiet`
- `cd web && npm run build`
- Browser smoke confirmed the `/docs` loading state renders endpoint-shaped skeleton rows before the OpenAPI spec resolves.
