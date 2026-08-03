---
title: Service Bus result prefix fallback
description: Recover completed Service Bus BLAST results when the sibling OpenAPI service writes to the legacy flat prefix despite receiving a date-tiered prefix hint.
tags: [blast, operate, ui]
---

# Service Bus result prefix fallback

## Motivation

Completed Service Bus request-queue jobs could show **Results are degraded** and
**Successfully parsed 0 of 0 result files** even though the BLAST execution had
succeeded.

Read-only live evidence for the same job showed three different facts:

- the durable dashboard row contained the intended prefix
  `2026/08/03/<openapi_job_id>/`;
- the sibling OpenAPI detail returned ten valid `result-001`…`result-010`
  files; and
- a direct Storage stream succeeded at
  `<openapi_job_id>/job-<elasticblast-id>/...out.gz`, while the date-tiered path
  had no file.

The sibling revision accepted the optional `results_prefix` submit field but
continued writing to its legacy flat job directory. Dashboard analytics trusted
the intended dated prefix, listed zero blobs, and baked that empty result into a
ready artifact. Subsequent page loads kept serving the cached 0-of-0 payload.

## User-facing change

Result discovery still tries the stored canonical prefix first. When that list
succeeds but is empty and differs from the legacy `<job_id>/` prefix, it retries
that exact flat prefix once. Explicit-prefix callers remain exact and never
fallback.

The result manifest and aggregate/alignments/taxonomy artifact schemas are
advanced so previously baked empty artifacts are marked stale and rebuilt once.
A truly empty result remains cacheable under the new schema; the change does not
create an unbounded rebuild loop.

## API and implementation summary

- Added one shared, bounded result-blob listing helper used by the result-file
  route, success-marker check, parser discovery, and artifact manifest builder.
- Native date-tiered jobs remain on their canonical path without a second list.
- Legacy and sibling-flat jobs are isolated by the existing collision-safe
  `<job_id>/` trailing-slash prefix.
- No Storage network setting, SAS path, RBAC assignment, Service Bus entity, or
  Azure resource was changed.

## Validation

- Live read-only sibling detail: ten result files on affected completed jobs.
- Live read-only Storage probes: flat candidate returned HTTP 200 and the dated
  candidate did not contain the file.
- `uv run pytest -q api/tests/test_external_date_layout.py api/tests/test_job_artifacts.py api/tests/test_blast_results_routes.py api/tests/test_storage_job_prefix.py api/tests/test_servicebus_tasks.py`
- `uv run ruff check api`
- `uv run pytest -q api/tests`
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict`
