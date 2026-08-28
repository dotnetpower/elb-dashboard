---
title: OpenAPI proxy schema cleanup
description: Keep internal multi-method reverse proxies out of the public FastAPI schema so every documented operation ID remains unique.
tags: [security, ui, architecture]
---

# OpenAPI proxy schema cleanup

## Motivation

FastAPI generated one identical operation ID for every method on each internal multi-method reverse proxy. Schema generation therefore emitted duplicate-operation warnings, and generated clients could not address those operations reliably. The routes are transport internals rather than public API contracts.

## User-facing change

The API Reference no longer lists the AKS Try-It transport proxy or the frontend static-asset catch-all. All documented API operations now have unique operation IDs. The API Reference Try-It workflow and normal SPA routing continue to use the same runtime routes.

## API and infrastructure summary

- `/api/aks/openapi/proxy` remains authenticated and routable but is excluded from the generated OpenAPI schema.
- `/{full_path:path}` remains the final frontend sidecar proxy but is excluded from the generated OpenAPI schema.
- No authorization, upstream allowlist, sidecar, role assignment, network, or Storage behavior changed.

## Validation

- OpenAPI, security-header, and route-contract regression tests run with `UserWarning` promoted to an error: 24 passed.
- The schema regression asserts both internal paths are absent and every remaining `operationId` is unique.
- Proxy security and Persona Matrix regression tests: 112 passed.
- Full backend validation: Ruff passed; 5,485 tests passed and 4 environment-only parity tests skipped.
- Full frontend validation: 109 files and 985 tests passed; ESLint and the production build passed; `npm audit` reported zero vulnerabilities.
- Local full-stack validation: 51 safe Playwright scenarios passed, 6 explicitly guarded live-mutation scenarios skipped, and the API smoke passed 27 of 27 checks.
- Documentation frontmatter and strict MkDocs builds passed; Bicep and control-plane JSON compilation passed.
