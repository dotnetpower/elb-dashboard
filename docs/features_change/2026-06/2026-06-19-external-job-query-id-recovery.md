---
title: Recover external-job Query ID from Storage when the Redis label is gone
description: The job detail view now derives the Query ID for an OpenAPI / Service Bus job from the durable query blob's first FASTA defline when the ephemeral Redis defline label has been evicted (e.g. by a Container App revision restart), so the header shows the real identity instead of "Query ID — ".
tags:
  - blast
  - ui
---

# Recover external-job Query ID from Storage when the Redis label is gone

## Motivation

After the [query-preview fix](2026-06-19-external-job-query-preview.md), the Run
details header for an external (OpenAPI / Service Bus) BLAST job still showed
**Query ID: —**. The inline-FASTA defline label for these jobs is remembered
only in **OPS Redis** (`elb:blast:extquery:<job_id>`, 7-day TTL). Redis is the
in-revision ephemeral sidecar, so **every Container App revision restart wipes
the label** — and the label is only persisted into the durable jobstate Table on
the next jobs-list sync. A job viewed after a restart (before that sync, or after
the label was already lost) therefore has no `query_label` and renders "—".

## User-facing change

On the **job detail view only**, when an external job has no resolvable
`query_label`, the backend now reads the first FASTA defline from the **durable
query blob** (`queries/<openapi_id>.fa` — the same blob the prepare-step preview
reads) and derives the Query ID from it. The recovered label is also
re-remembered in Redis so the next jobs-list sync persists it back to the Table
row (durable thereafter). The header shows e.g. `Query ID: sb-e2e-q1` instead of
"—".

This is **detail-view only** (one Storage read, capped to 512 bytes, only when
the label is missing) — never on the jobs LIST path, which would be one read per
row.

## API / IaC diff summary

No HTTP contract change (`query_label` is an existing response field). Internal:

- `api/services/blast/job_state.py` — new `derive_external_query_label(job_id, caller)`:
  resolves the durable query blob via the existing `_job_query_blob_path`
  external-reconstruction, reads ≤512 bytes from the `queries` container, and
  derives the label via `external_query_labels.derive_inline_query_label`.
  Returns `""` for non-external jobs, an unresolvable/unreadable blob, or a
  header-less FASTA; never raises except the ownership 403.
- `api/routes/blast/jobs.py` — `blast_job_get` calls the helper to backfill
  `query_label` when empty, and re-remembers it in Redis (best-effort) so the
  next sync persists it to the Table.

## Validation evidence

- `uv run ruff check api` — clean.
- New `test_job_detail_recovers_query_label_for_external_job` (external job, no
  `query_file`, reads `queries/openapi-xyz.fa`, header `query_label` == derived
  `myquery`).
- Wider sweep `test_blast_jobs_routes.py test_smoke.py test_external_query_labels.py test_local_to_blast_job.py test_external_job_projection.py`
  — 182 passed. The existing dashboard-job detail test is unchanged (it carries a
  `query_file`, so the recovery fallback never fires — no regression).

## Scope note

The fix covers the synced-Table detail branch (the reported case). A
not-yet-synced external job served directly from the sibling still relies on the
remembered Redis label (or shows "—" until the first sync), unchanged — that
rarer path already runs `apply_remembered_query_label`.
