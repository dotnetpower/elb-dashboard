---
title: External BLAST submit accepts the documented string outfmt "5"
description: Coerce outfmt "5" (string) to int 5 so OpenAPI /v1/jobs example payloads work on the dashboard submit and Service Bus bridge paths.
tags:
  - blast
  - api
---

# 2026-06-14 — External BLAST submit accepts string `outfmt: "5"`

## Motivation

During a live end-to-end validation of the Service Bus → OpenAPI bridge, a
BLAST request message whose `options.outfmt` was the JSON **string** `"5"` was
silently dead-lettered. Root cause: the dashboard's `ExternalBlastOptions.outfmt`
was typed `Literal[5]` (int only) and Pydantic v2 does not coerce the string
`"5"` to int `5` for a `Literal`. The message therefore failed validation and the
Service Bus drain handler dead-lettered it.

This is a contract inconsistency, because the string form is exactly what the
platform itself documents:

* The sibling OpenAPI `/v1/jobs` examples (Mode B / Mode B+taxid) use
  `"blast_options": {"outfmt": "5"}` — and the sibling plane **accepts** it (a
  live taxid submit with string `"5"` ran to completion).
* The dashboard's own API Reference spec (`web/src/pages/apiReference/spec.ts`)
  emits `outfmt: "5"` (string), and `spec.test.ts` asserts it.

So a Service Bus producer (or any API caller) copying the documented example
verbatim was rejected on the dashboard bridge / `POST /api/v1/elastic-blast/submit`
path, even though the same payload works against `/v1/jobs`.

## User-facing change

* `POST /api/v1/elastic-blast/submit` and the Service Bus request bridge now
  accept `outfmt` as either int `5` or the documented string `"5"`, coercing the
  string form to int `5`. Payloads copied from the OpenAPI `/v1/jobs` examples
  now succeed instead of being dead-lettered.
* The XML-only contract is unchanged: any non-`5` value (`6`, `"6"`, `"7"`,
  `"xml"`, …) still fails validation with HTTP 422, because the result pipeline
  requires BLAST XML (format 5).
* Dead-lettered messages remain visible to the operator (DLQ count + warning on
  the BLAST Jobs page Service Bus strip, the Message Flow modal, and the Settings
  → Service Bus section), so this fix reduces false dead-letters without hiding
  any genuine failure.

## API / IaC diff summary

No API surface or IaC change. Internal only:

* `api/routes/elastic_blast.py` — `ExternalBlastOptions` gains a
  `field_validator("outfmt", mode="before")` that coerces the string `"5"` to
  int `5`; the field type stays `Literal[5]`.

## Validation evidence

* Live root-cause capture: a Service Bus message with `outfmt: "5"` was
  dead-lettered (`dead_letter_reason=handler_rejected`); the same payload with
  int `5` drained → OpenAPI job `7cf27135db1f` ran to **completed**. The Mode
  B+taxid example (`taxid=562`, `is_inclusive=true`) submitted directly to
  `/v1/jobs` with string `"5"` ran to completion (job `44ebe8d89f28`).
* `uv run pytest -q api/tests` → **3549 passed, 3 skipped**.
* New tests in `api/tests/test_external_blast_api.py`:
  * `test_external_blast_accepts_string_outfmt_five` — `"5"`→`5`, non-XML still rejected.
  * `test_external_blast_submit_accepts_string_outfmt_five` — submit with string
    `"5"` returns HTTP 202 and forwards int `5` upstream.
* `uv run ruff check api/routes/elastic_blast.py` → clean.

## Note

This fix is not yet deployed; the live Service Bus bridge keeps rejecting string
`outfmt` until the api sidecar is redeployed. The dead-letter remains
operator-visible in the meantime, so no message is silently lost.
