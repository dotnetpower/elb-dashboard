---
title: Thread cluster scope through external BLAST facade outbound calls
description: >-
  External BLAST facade routes and fallback result/status paths now accept
  optional cluster scope and forward it to outbound OpenAPI ready/get/stream
  calls so per-cluster base URL and API token resolution can be applied.
tags:
  - blast
  - security
---

# Thread cluster scope through external BLAST facade outbound calls

## Motivation

Issue [#26](https://github.com/dotnetpower/elb-dashboard/issues/26) tracks outbound
OpenAPI calls that still resolve against global runtime keys when the call site
has no cluster context. The core resolver already supports per-cluster context,
but several facade and fallback routes still called it without threading scope.

## User-facing change

Routes that can be cluster-scoped now accept optional
`subscription_id` / `resource_group` / `cluster_name` and forward those values
through outbound OpenAPI calls.

- `POST /api/v1/elastic-blast/submit` forwards scope to `external_blast.ready`
  and `external_blast.submit_job`.
- `GET /api/v1/elastic-blast/jobs/{job_id}`, `/events`, `/manifest`, and
  `/files/{file_id}` forward scope to `external_blast.get_job` / `stream_file`.
- `GET /api/v1/elastic-blast/jobs` accepts optional scope and forwards it to the
  cached external list fetch.
- Fallback paths in `/api/blast/jobs/{job_id}` and `/api/blast/jobs/{job_id}/results`
  (including result file streaming) now pass provided scope when calling
  external OpenAPI.

All added parameters default to empty values, so existing callers keep the
legacy behavior unless they provide scope explicitly.

## API / IaC diff summary

- Updated route handlers:
  - [api/routes/elastic_blast.py](../../../api/routes/elastic_blast.py)
  - [api/routes/blast/jobs.py](../../../api/routes/blast/jobs.py)
  - [api/routes/blast/results.py](../../../api/routes/blast/results.py)
- Updated external client entry points to thread scope:
  - [api/services/external_blast.py](../../../api/services/external_blast.py)
- Added route-level regression tests in
  [api/tests/test_external_blast_api.py](../../../api/tests/test_external_blast_api.py).
- No IaC change. No new dependency.

## Validation evidence

- `.venv/bin/ruff check api/services/external_blast.py api/routes/elastic_blast.py api/routes/blast/jobs.py api/routes/blast/results.py api/tests/test_external_blast_api.py` — clean.
- `.venv/bin/pytest -q api/tests/test_external_blast_api.py api/tests/test_external_blast_cluster_resolver.py` — 108 passed.
- `.venv/bin/pytest -q -n 0 api/tests` — 3832 passed, 3 skipped, 79 deselected.
