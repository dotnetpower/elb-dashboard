# API Reference Page — Premium Glassmorphic Redesign

**Date**: 2026-05-11

## Motivation

The API Reference page previously used an iframe to embed Swagger UI from the OpenAPI pod. This was fragile (CORS issues, no customization) and didn't match the glassmorphic design language of the rest of the app.

## User-facing Change

- New `/docs` route replaces `/api-reference` (Vite proxy conflict resolved)
- Hero header with gradient accent, version badge, and endpoint/group/method stats
- 2-column card layout: left = documentation (params, responses), right = Try-it panel
- **Instant execution**: GET endpoints with no required params execute immediately on "Try" click
- Lightweight JSON syntax highlighting in response viewer (keys=blue, strings=green, numbers=sand, booleans=purple, null=grey italic)
- Method badges with per-method glow colors
- Copy response button with status code coloring and response time display

## API / IaC Diff

- `web/src/pages/ApiReference.tsx` — complete rewrite (~550 lines)
- `web/vite.config.ts` — proxy pattern `/api` → `/api/` (trailing slash) to prevent route collision

## Validation

- TypeScript build: 0 errors
- Browser: all 9 OpenAPI endpoints tested via Try-it, responses rendered with syntax highlighting
