---
title: Failed-job error rendering — clamp error_code and the Recent searches note
description: Stop storing a 700+ char elastic-blast error body in the job error_code field, and clamp the failed-job error so it no longer blows out the Recent searches table row.
tags:
  - blast
  - user-guide
---

# Failed-job error rendering — clamp error_code and the Recent searches note

## Motivation

A failed `core_nt` job (memory-exceeded) rendered as a broken row on the Recent
searches page: the job's full multi-line BLAST runtime error — including a
REDACTED Azure `x-ms-*` header dump, ~770 characters — was shown inline as the
query subtitle and stored verbatim in the job's `error_code` field. `error_code`
is supposed to be a short, greppable identifier (`database_not_found`,
`worker_lost`), not a paragraph.

## Root causes (two layers)

1. **Backend** (`api/services/blast/external_jobs.py`): `_external_error_message`
   accepted whatever was in an external job's `error` field. elastic-blast
   reports failures as a free-form string (or a dict whose `code` is the entire
   error body), so the full text landed in both `error` and `error_code`.
2. **Frontend** (`web/src/pages/BlastJobs/JobRow.tsx`): the row rendered
   `view.note` (which is `j.error`) as a raw `<span>` with no clamping, so the
   700+ char string overflowed the table layout.

## User-facing change

- Recent searches failed rows now show a single-line, length-clamped error
  summary (the `ERROR:` prefix stripped, whitespace collapsed). The full error
  remains available on hover via the row's `title` tooltip.
- The job `error_code` is now always a short token. A long error body is kept as
  the (capped) `error_message` instead of being mis-stored as a code.

## API / IaC diff summary

- `api/services/blast/external_jobs.py`: `_external_error_message` now routes a
  "code" candidate through `_normalise_error_code` (rejects anything with
  whitespace or > 80 chars) and the message through `_clamp_error_message`
  (whitespace-collapse + 2000-char cap with an ellipsis). String errors never
  populate `error_code`. No response field added/removed — only the values are
  sanitised.
- `web/src/pages/BlastJobs/JobRow.tsx`: reuses the existing `summariseNote`
  helper to clamp the note to one line, with the full text on hover.
- No infra change.

## Validation evidence

- Live root cause: `GET /api/blast/jobs` job `e2a80869` had `error_code` and
  `error` both 772 chars containing the `x-ms-*` dump.
- New backend test `test_external_error_message_rejects_long_body_as_code`
  covers: plain-string error, dict-with-long-code, real short code, and empty.
- `uv run pytest -q api/tests/test_external_blast_api.py` → 64 passed.
- `cd web && npx vitest run src/components/cards` → 97 passed;
  `npx tsc --noEmit` clean; `npm run build` succeeds.
