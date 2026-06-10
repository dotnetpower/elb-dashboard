---
title: Derive OpenAPI submit idempotency key from correlation id to prevent duplicate jobs
description: Forwarded BLAST submits without a caller idempotency_key now derive one from external_correlation_id, so a retried submit dedupes to one cluster job instead of creating duplicates under burst load.
tags:
  - blast
  - security
---

# Derive OpenAPI submit idempotency key from correlation id

## Motivation

When many `POST /api/blast/jobs` (and `/api/v1/elastic-blast/submit`) requests
arrive almost simultaneously, the dashboard forwards each to the sibling
`elb-openapi` `POST /v1/jobs` execution plane via
`api/services/external_blast.py::submit_job`. The sibling runs a **single
uvicorn worker** (one pod, `replicas: 1`) and its `submit_job` handler performs
blocking I/O (azcopy upload, `az login`, ConfigMap writes) directly on the event
loop, so near-simultaneous submits are **serialised**. Under a burst the later
clients can hit the `_DEFAULT_TIMEOUT_SECONDS` (90 s) `ReadTimeout` *after* their
job was already created on the cluster.

`submit_job` retries transient transport failures (up to
`_SUBMIT_MAX_TRANSPORT_RETRIES`) — but the sibling dedupes a re-send **only** on
`idempotency_key`. It deliberately does **not** treat `external_correlation_id`
as a dedupe key (sibling test `test_external_correlation_id_is_not_idempotency_key`).

The bug: the retry guard keyed `has_idempotency_key` off
`idempotency_key OR external_correlation_id`, while `canonical_submit_metadata`
only sets `idempotency_key` when the **caller** supplies it (it always sets a
unique `external_correlation_id`). So a normal SPA submit was considered
"retry-safe" but carried no key the sibling would dedupe on — and each retry of a
lost-response submit minted a **new** `uuid` job id, creating a **duplicate
BLAST job per retry**.

## User-facing change

- Concurrent / retried BLAST submits no longer create duplicate cluster jobs.
  A submit whose first attempt succeeded but whose response was lost to a
  timeout is collapsed to the same job on retry.
- No SPA change and no API contract change — the dashboard still returns one
  `job_id` per logical submit, now reliably so under load.

## API / task diff summary

- `api/services/external_blast.py::submit_job`: when the forwarded payload has no
  `idempotency_key`, derive one from `external_correlation_id` on a **copy** of
  the payload (never mutate the caller's dict). The retry guard now keys on the
  real `idempotency_key` only. A caller-supplied `idempotency_key` still wins.
- No change to the shared `canonical_submit_metadata` (it also feeds the local
  Celery `_normalise_blast_submit_body` dedup path, which must keep its existing
  semantics).

## Validation evidence

- New regression tests in `api/tests/test_external_blast_api.py`:
  - `test_submit_job_derives_idempotency_key_from_correlation_id`
  - `test_submit_job_does_not_mutate_caller_payload`
  - `test_submit_job_preserves_caller_idempotency_key`
  - `test_submit_job_retry_resends_same_idempotency_key`
  - `test_submit_job_without_any_key_does_not_retry`
- Queue-invariant probe against the real sibling ASGI app on a single event
  loop: 12 concurrent distinct submits → 12 distinct jobs, no loss; active count
  never exceeded `MAX_ACTIVE_SUBMISSIONS`; queue positions monotonic; 10
  concurrent submits sharing one `idempotency_key` → exactly 1 job.
- `uv run pytest -q api/tests`: 3256 passed, 3 skipped.
- `uv run ruff check api/services/external_blast.py api/tests/test_external_blast_api.py`: clean.

## Out of scope (sibling perf, tracked separately)

The sibling serialises burst submits because its `async def` submit handler does
blocking I/O on the event loop. That is a **performance** concern in the
read-only `elastic-blast-azure` repo; this fix neutralises the *correctness*
consequence (duplicate jobs). Offloading the sibling's upload/login/persist to a
threadpool would reduce burst submit latency but is a separate cross-repo change.
