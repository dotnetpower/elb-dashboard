---
title: Service Bus completion events carry the real BLAST failure cause
description: A failed blast.transition event now enriches the sibling's coarse error with the authoritative cluster-side blastn failure detail.
tags:
  - blast
  - operate
---

# Service Bus completion failure detail

## Motivation

A Service Bus consumer that submitted a job through the request queue only ever
saw a coarse, non-actionable error on the `failed` `blast.transition` completion
event:

```json
{ "status": "failed", "error_code": "failed", "error_message": "one or more BLAST jobs failed" }
```

The sibling OpenAPI service stamps that generic string because it only knows the
Kubernetes Job failed — it does not read the elastic-blast runner's blastn
stderr / exit code. The dashboard's own detail view already recovers the real
cause from the workload results container (`metadata/FAILURE.txt` +
`logs/BLAST_RUNTIME-NNN.out`) via `_enrich_external_failure_detail`, but the
Service Bus completion path never applied that enrichment, so a queue-first
subscriber could not tell *why* a job failed.

## User-facing change

A `failed` completion event now carries the authoritative cluster-side detail in
`error_message` when the sibling error is coarse/generic (or empty):

```json
{
  "status": "failed",
  "error_code": "failed",
  "error_message": "BLAST search exited with code 2: Input db vol does not match lmdb vol"
}
```

* `error_code` is unchanged (still the machine-readable reason).
* A genuinely specific sibling error is left untouched — enrichment only fires
  for the known coarse strings (`one or more BLAST jobs failed`, a bare
  Kubernetes `CrashLoopBackOff`, …) or an empty error.
* The detail is sanitised (charter §12) and length-bounded before it reaches the
  topic envelope.

## API / IaC diff summary

* `api/tasks/servicebus/tasks.py`
  * New `_enrich_failure_message_for_event(job, openapi_job_id, current_error)` —
    resolves the workload Storage account from the sibling job's `db` blob URL
    through `extract_trusted_storage_account` (the same trust gate the dashboard
    uses, so an attacker-influenced `db` URL can never redirect the shared MI
    Storage token to a foreign account), then reuses
    `_enrich_external_failure_detail` to read the cluster-side detail.
  * `_publish_one_bridge` calls it on a `failed` transition and replaces the
    coarse `error_message` when a better detail is available. Best-effort:
    returns `None`/keeps the coarse message on any failure.
* No IaC change.

## Validation

* `uv run pytest -q api/tests/test_servicebus_tasks.py` — 4 new tests
  (`test_enrich_failure_message_recovers_cluster_detail`,
  `test_enrich_failure_message_keeps_specific_error`,
  `test_enrich_failure_message_untrusted_account_skips`,
  `test_publish_transitions_failed_enriches_coarse_error`) plus the existing
  suite, all green.
* `uv run ruff check api/tasks/servicebus/tasks.py` — clean.
