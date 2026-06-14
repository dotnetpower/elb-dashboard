---
title: Message Flow card surfaces direct /v1/jobs submissions
description: The Message Flow card now syncs external OpenAPI /v1/jobs jobs into the Table before reading it, so directly-submitted jobs appear without opening Recent searches.
tags:
  - ui
  - blast
---

# Message Flow card surfaces direct `/v1/jobs` submissions

## Motivation

Jobs submitted directly through the sibling OpenAPI plane (`POST /v1/jobs`) are
handled by the in-cluster `elb-openapi` pod and never create a dashboard
`jobstate` Table row on their own. The Message Flow card draws **active Table
rows** (not Service Bus queue contents), so a `/v1/jobs` job was invisible on
the card until the operator happened to open the Recent searches list — the only
code path that discovered external jobs and synced them into the Table.

A secondary defect: once such a row *was* synced, its producer lane mislabeled
the submitter as a dashboard user (the synced payload is `{"external": job}`
with no top-level `submission_source`, so the message-flow source resolver fell
through to its `dashboard` default).

## User-facing change

* Opening the dashboard now shows directly-submitted `/v1/jobs` jobs on the
  Message Flow card (and its expanded constellation) without first visiting
  Recent searches. The card best-effort pulls external jobs for the platform
  subscription (`AZURE_SUBSCRIPTION_ID`) into the Table before rendering.
* Those jobs label their producer lane as **external** instead of a dashboard
  user.
* The card never breaks if external discovery/sync fails — it degrades to the
  locally-known rows (the sync is best-effort and bounded).

## API / IaC diff summary

No HTTP contract or response-shape change; no IaC change.

* `api/services/blast/external_jobs.py` — new reusable
  `collect_and_sync_external_jobs(...)` (+ `ExternalJobsSyncResult`) that owns
  OpenAPI target resolution, per-cluster `/v1/jobs` discovery, scope-default +
  query-label application, optional detail enrichment, and the idempotent Table
  upsert. The target-resolution helpers (`_resolve_external_list_targets`,
  `_external_row_with_scope_defaults`, `_external_list_row_needs_detail`) moved
  here from the jobs route so both callers share one orchestration and cannot
  drift.
* `api/routes/blast/jobs.py` — `_compute_blast_jobs_response` now calls the
  shared function instead of an inline copy of the discovery+sync logic; the
  route keeps its tombstone filter, row merge, and degraded-badge policy
  (behaviour unchanged, verified by the existing route tests).
* `api/services/message_flow.py` — `build_message_flow` gained a defaulted
  `tenant_id` kwarg and a best-effort `_sync_external_jobs_best_effort()` call
  (detail enrichment disabled) before reading the Table; `_submission_source`
  now recognises a `payload["external"]` block as `external_api`.
* `api/routes/monitor/message_flow.py` — passes the caller tenant through.

## Validation evidence

* `uv run pytest -q api/tests` → **3510 passed, 3 skipped**.
* `uv run ruff check api` → clean.
* New tests:
  * `test_external_synced_row_labels_producer_external`,
    `test_build_message_flow_syncs_external_jobs`,
    `test_build_message_flow_sync_is_best_effort`,
    `test_build_message_flow_skips_sync_without_subscription`
    (`api/tests/test_message_flow.py`).
  * `test_collect_and_sync_external_jobs_discovers_and_upserts`,
    `test_collect_and_sync_external_jobs_never_raises`
    (`api/tests/test_external_blast_api.py`).
* The five existing `test_canonical_jobs_list_*` route tests were repointed to
  patch the resolver/discovery functions on `api.services.blast.external_jobs`
  (their new home) and still assert the same captured base-url/token and
  degraded behaviour.

## Known scope limitation

The card discovers clusters in the platform subscription
(`AZURE_SUBSCRIPTION_ID`) only. `/v1/jobs` jobs running on clusters in a
*different* subscription still appear on the per-subscription Recent searches
view but not on the dashboard-wide Message Flow card. This matches the
single-tenant-per-deployment model where clusters live in the platform
subscription.
