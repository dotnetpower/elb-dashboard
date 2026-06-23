---
title: Age-based retention purge + blob soft-delete + cost lifecycle — #76
description: A default-OFF daily retention task that purges + tombstones completed jobs older than BLAST_RESULT_RETENTION_DAYS via the #69 recursive delete, plus the blob soft-delete safety net and a Cool-tier cost lifecycle in Bicep.
tags:
  - storage
  - infra
---

# Retention purge + blob soft-delete + cost lifecycle

Epic #64, issue #76 (the #69 follow-up). With this, the dfs storage migration
epic is complete.

## What landed

### 1. Age-based retention purge (default-OFF)

`api/services/storage/retention.py` `purge_aged_results(days=, dry_run=, limit=)`
+ the beat task `api.tasks.storage.purge_aged_results` (daily):

- **Gated** on `STORAGE_DFS_ENABLED` AND `BLAST_RESULT_RETENTION_DAYS > 0`
  (default `0` = disabled). A cheap no-op every tick until an operator opts in.
- For completed jobs older than the window (by `updated_at`/`created_at`), it
  calls the #69 best-effort recursive `purge_job_result_storage(state)` then
  tombstones the row (`status=deleted`) — same end state as a user delete, so the
  job catalog stays consistent. `dry_run=True` (the service default) reports the
  plan without touching anything.
- **Idempotent**: already-`deleted` rows skipped; bounded by `limit`; never raises
  per job.

### 2. Blob soft-delete safety net (Bicep)

`infra/modules/storage.bicep` adds `blobServices` with `deleteRetentionPolicy` +
`containerDeleteRetentionPolicy` (7 days). The dfs recursive delete is irreversible
at the API level; soft-delete keeps deleted result/query blobs recoverable for 7
days — **the guardrail required before `STORAGE_DATE_LAYOUT_ENABLED` / retention is
enabled**.

### 3. Cost lifecycle (Bicep)

A `managementPolicies` rule tiers `results/` block blobs to **Cool** after 30 days
of no modification. Tiering only — **no delete** (retention is the app task, which
keeps the catalog consistent) and **no Archive** (Cool stays directly readable; no
rehydration). This is the standard "archive" approach; a dfs directory rename to an
`archive/` prefix was intentionally NOT used (it would change the row's prefix and
add read-path complexity for no benefit over lifecycle tiering).

## Validation evidence

- `uv run pytest api/tests/test_retention.py` → **7 passed** (default 0; disabled
  when window 0 / dfs off; dry-run plans without purging; live purges+tombstones
  aged + skips recent; skips already-deleted; records error without raising).
- `az bicep build --file infra/main.bicep` → compiles (soft-delete + lifecycle).
- `uv run pytest api/tests/test_tasks_facade_contract.py` → **55 passed** (the new
  task is exported); task registered as `api.tasks.storage.purge_aged_results`.
- `uv run ruff check api` → clean.

## Deploy note

The Bicep additions (soft-delete + lifecycle) take effect on the next
`azd provision`; both are supported on HNS (ADLS Gen2) accounts and are validated
there. The retention task ships scheduled but disabled — flip
`BLAST_RESULT_RETENTION_DAYS` (with `STORAGE_DFS_ENABLED`) only after soft-delete is
confirmed live and with sign-off in a shared/customer environment.

## Self-critique (design pass)

- **Safety**: blob soft-delete (7d recoverable) backs the irreversible delete;
  retention default-OFF; tombstone matches user-delete semantics. ✓
- **Idempotency / partial failure**: deleted rows skipped; per-job try/except;
  bounded by `limit`. ✓
- **Lifecycle**: Cool-tier only (readable), no Archive/rehydration, no
  lifecycle-delete (avoids catalog/blob desync). ✓
- **Backward-compat**: all default-OFF; Bicep additive. ✓
- Verdict: no Critical/High; deletion of user data stays operator-gated.
