# Live pod logs for external / Service Bus jobs via sibling-exposed elb_job_id

**Date:** 2026-06-17
**Area:** Job log streaming (`api/services/job_logs/k8s.py`); paired sibling change in `dotnetpower/elastic-blast-azure`

## Motivation

External / Service Bus jobs showed the execution **step timeline** (preparing /
configuring / submitting / running) over SSE, but never streamed the **raw
Kubernetes pod log lines** the way dashboard-native jobs do. Root cause: the
dashboard follows BLAST pods by the elastic-blast `elb-job-id` label
(`job-<hash>`), but it only knows the sibling's OpenAPI `job_id`
(`607f122b…`-style) and had no way to map one to the other — the sibling's
`/v1/jobs` API did not expose the `elb-job-id`.

## Change

Two paired changes close the gap:

1. **Sibling (`elastic-blast-azure`, committed/pushed by the maintainer)** —
   `/v1/jobs` (list) and `/v1/jobs/{id}` (detail) now expose `elb_job_id`. The
   sibling already discovers and stores it from the `elastic-blast submit`
   output (`_effective_elb_job_id`); the public payload just surfaces it, guarded
   to emit only a *genuine* discovered id (never the OpenAPI `job_id` fallback).

2. **Dashboard (this repo)** — `resolve_elastic_blast_job_id` now reads
   `payload.external.elb_job_id`. The external sync already stores the whole
   sibling row under `payload.external`, so once the row carries `elb_job_id`
   the resolver returns it and `k8s_follow_manager` discovers the pods by their
   `elb-job-id` label and streams their logs — the same live-log experience as a
   dashboard-native job.

## Compatibility

The dashboard change is additive and safe to ship independently: when the
sibling does not expose `elb_job_id` (pre-deploy), the field is absent and the
resolver falls through to its existing candidates (returns `""`, unchanged
behaviour). Live external-job logs activate only after the sibling image is
rebuilt and deployed (a maintainer action).

## Validation

- Dashboard: `uv run pytest -q -n 0 api/tests/test_job_log_k8s.py` — 8 passed
  (incl. the new `test_resolve_elastic_blast_job_id_reads_external_elb_job_id`);
  `uv run ruff check api/services/job_logs/k8s.py` — clean.
- Sibling: `docker-openapi/.venv/bin/python -m pytest tests/` — 75 passed (incl.
  the two new `test_external_job_payload_*_elb_job_id` tests).

## Follow-up

The live effect requires rebuilding + deploying the sibling `elb-openapi` image
(consequential — maintainer's call). Until then, external/Service Bus jobs keep
showing the step timeline; the raw pod logs light up after the sibling deploy.
