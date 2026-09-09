---
title: OpenAPI compatibility gate and generated TypeScript declarations
description: Detect breaking API changes and keep a separate generated type namespace without replacing existing runtime clients.
tags: [contributor, architecture]
---

# OpenAPI compatibility gate and generated TypeScript declarations

## Motivation

FastAPI generated operation identifiers and schemas, while frontend API types
were maintained manually. A function rename, enum narrowing, or newly-required
field could break external clients without an explicit review gate. Generated
types were also unavailable to new frontend surfaces.

## Change

- A deterministic structural baseline records 242 operations and component
  schemas while ignoring documentation-only text.
- CI detects removed operations/properties, changed operation ids, narrowed
  enums, tightened numeric/string/collection constraints, removed union
  variants, added `allOf` constraints, constrained map values, newly-required
  inputs, removed response media types, and changed global or operation
  security requirements.
- Intentional breaking changes require an explicit breaking commit marker.
- `openapi-typescript` 7.13.0 generates a declaration-only namespace at
  `web/src/api/generated/openapi.d.ts`.
- Generation invokes only the pinned local executable and fails if `npm ci`
  has not installed it; it never performs an implicit network install.
- Generation is byte-deterministic and a fail-closed checker catches missing,
  untracked, or stale declarations.
- The existing hand-written API clients and imports are unchanged.

The generator's transitive `js-yaml` dependency is overridden to patched
version 4.3.2; `npm audit` reports zero vulnerabilities.

## Runtime and Service Bus impact

All work runs at build or test time. No FastAPI route, request/response body,
authentication path, Service Bus queue, completion topic, or Container App
environment value changes at runtime.

## Validation

- `uv run pytest -q api/tests/test_openapi_contract.py` - 8 passed.
- `uv run python scripts/dev/check_openapi_contract.py` - 242 operations unchanged.
- Source bootstrap comparison - 238 pre-expansion operations to 242 current,
  zero breaking changes.
- `npm --prefix web run check:api-types` - generated declarations current.
- `npm --prefix web audit` - zero vulnerabilities.
- ShellCheck passed for generation/check and pre-push scripts.