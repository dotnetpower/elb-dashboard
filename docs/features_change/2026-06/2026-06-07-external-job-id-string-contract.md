---
title: External job projection always emits a string job_id
description: The OpenAPI→dashboard job projection now emits an empty-string job_id instead of null when the upstream omits it, matching the SPA's non-nullable BlastJobSummary.job_id and preventing /blast/jobs/null navigation.
tags:
  - operate
---

# External job projection always emits a string job_id (2026-06-07)

## Motivation

`api/services/blast/external_job_projection.py::_external_to_blast_job`
projects a raw OpenAPI job dict into the dashboard's `BlastJobSummary` shape.
It already normalises the OpenAPI id to a string
(`openapi_job_id = str(job.get("job_id") or "")`), but the projected
`out["job_id"]` was set from the **raw** `job.get("job_id")`, which is `None`
when the upstream response omits the field.

The SPA types `BlastJobSummary.job_id` as a non-nullable `string`
(`web/src/api/blast.types.ts`) and uses it directly for navigation and React
list keys — e.g. `` `/blast/jobs/${encodeURIComponent(job.job_id)}` `` becomes
`/blast/jobs/null` and `key={job.job_id}` becomes a `null` key. A `None`
job_id therefore produces broken navigation and React key warnings.

## User-facing change

A job whose upstream OpenAPI record lacks an id now surfaces with an empty
`job_id` (consistent with the already-normalised `openapi_job_id`) instead of
`null`, so the jobs list no longer produces `/blast/jobs/null` links.

## API / IaC diff summary

- `api/services/blast/external_job_projection.py` — `_external_to_blast_job`:
  `out["job_id"]` now reuses the already-normalised `openapi_job_id`
  (`str(job.get("job_id") or "")`) instead of the raw `job.get("job_id")`.
- No IaC change.

## Validation evidence

- `uv run pytest -q api/tests/test_external_job_projection.py` — 2 passed
  (`test_external_job_id_is_always_a_string_when_present`,
  `test_external_job_id_falls_back_to_empty_string_when_missing`).
- `uv run ruff check api/services/blast/external_job_projection.py api/tests/test_external_job_projection.py` — clean.
