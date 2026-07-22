---
title: Service Bus send returns per-field detail on a 4xx
description: A rejected request-queue send (400) now carries structured loc/msg/type field errors instead of a single truncated blob.
tags:
  - blast
  - user-guide
---

# Service Bus send returns per-field detail on a 4xx

## Motivation

When enqueueing a BLAST request onto the Service Bus request queue via the
Playground producer (`POST /api/settings/service-bus/send`), a body that failed
validation was rejected with:

```json
{ "code": "invalid_request", "message": "<str(ValidationError)>[:400]" }
```

`str()` on a Pydantic v2 `ValidationError` is a multi-line human blob that was
truncated at 400 characters, so a body failing several fields lost detail and
was not machine-parseable. The caller could not reliably tell **which** field
failed or why — they had to guess and retry.

The synchronous producer paths are the only place a 4xx can be *returned* to the
sender: the asynchronous drain → OpenAPI submit path already returned
`202 queued` before the sibling ever sees the request, so a drain-time 4xx is
surfaced through a failure event / failed job row, not an HTTP response.

## User-facing change

A rejected send now returns the **same diagnosable shape the native FastAPI
submit route already emits** — a structured `errors` list plus a summary
`message` that names the offending field(s):

```json
{
  "code": "invalid_request",
  "message": "query_fasta: Field required; program: Input should be 'blastn', ...",
  "errors": [
    { "loc": "query_fasta", "msg": "Field required", "type": "missing" },
    { "loc": "program", "msg": "Input should be 'blastn', ...", "type": "literal_error" }
  ]
}
```

Backward compatible: `code` and `message` are preserved (the SPA
`formatApiError` reads the top-level `message`), and `errors` is additive. The
field detail exposes only `loc`/`msg`/`type` — never the submitted `input`/`ctx`
values, which can carry the query FASTA or other user content. The error list is
capped at 20 entries.

## API / IaC diff summary

* [api/routes/settings/service_bus.py](../../../api/routes/settings/service_bus.py)
  — new `_format_validation_errors()` helper; `_validate_send_body()` now catches
  `pydantic.ValidationError` and returns `{code, message, errors}` (the generic
  `Exception` fallback is unchanged). New `_MAX_VALIDATION_ERRORS = 20` cap.
* No IaC change. No new dependency (`pydantic` already a runtime dependency).

## Validation evidence

* `uv run pytest -q api/tests/test_settings_service_bus.py` — 29 passed
  (updated `test_send_invalid_body_returns_400`, new
  `test_send_invalid_body_reports_every_failing_field`).
* `uv run pytest -q api/tests/test_settings_service_bus.py api/tests/test_servicebus_v1_multitoken.py api/tests/test_external_blast_api.py`
  — 183 passed.
* `uv run ruff check api/routes/settings/service_bus.py` — clean.
