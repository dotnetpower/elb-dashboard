---
title: Service Bus oversized request preflight
description: Preserve deterministic HTTP 413 responses by validating the legacy request wire size before the queue-capacity circuit.
tags: [blast, operate]
---

# Service Bus oversized request preflight

## Motivation

The Service Bus Playground route checked queue capacity before serializing the request body. In a fail-closed deployment whose management-plane count circuit was already open, an oversized request could therefore return HTTP 503 before reaching the existing 192 KiB wire-size guard. GitHub Actions exposed the ordering dependency when the oversized-request test received 503 instead of its contractually expected 413.

## User-facing change

Oversized requests now return HTTP 413 with `code=request_too_large` before any queue-capacity lookup or broker operation. Valid requests still pass through the existing capacity circuit and send path unchanged.

## API and infrastructure summary

- The shared preflight uses the exact legacy serializer, `json.dumps(body, default=str)`, and the existing byte limit.
- `send_request` uses the same helper, so request body bytes, content type, subject, initial MessageId, correlation ID, TTL, and retry MessageId behavior are unchanged.
- No Service Bus entity, authentication mode, token, RBAC assignment, Storage rule, network setting, or frontend contract changed.

## Validation

- The route test forces fail-closed threshold 1 and makes any capacity lookup raise; the oversized body still returns HTTP 413.
- `uv run pytest -q api/tests/test_settings_service_bus.py api/tests/test_service_bus_drain_loop.py` - `85 passed`.
- `CI=true uv run pytest -q api/tests` - `5312 passed, 4 skipped`.
- `uv run ruff check api` - passed.
- `uv run python scripts/docs/check_frontmatter.py` and `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.