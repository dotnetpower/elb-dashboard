---
title: Queue/API job details — sibling stats, immediate region, query identity, BLAST command + raw params
description: Follow-ups to the queue/API parameter capture — merge live sibling stats (DB/BLAST version, run time) on the detail, stamp the cluster scope at drain so region populates immediately, capture query length/molecule from the FASTA, and render a BLAST command preview plus a raw-parameters panel.
tags:
  - blast
  - ui
---

# Queue/API job detail completeness — 4 follow-ups (2026-06-24)

Builds on the earlier "full BLAST parameters + region" change. Four follow-ups
so a Service Bus / external-API job detail is as complete as a dashboard-native
job.

## 1. Live sibling stats on the detail

The sibling `/v1/jobs` record carries `db_version`, `blast_version`,
`run_seconds`, `queue_wait_seconds`, `elapsed_seconds` but the dashboard's stored
row did not. The detail route (`blast_job_get`) now merges them for a
**completed** external job that has none — one best-effort call, gated to
terminal-success (so it never double-fetches a failed job whose error path
already fetched it) and **cached in OPS Redis** so a stopped-cluster job's detail
does not re-pay a 10 s timeout on every open.

## 2. Immediate region (drain-time cluster scope)

The SB config routing is often blank, which left a freshly-drained row with no
cluster scope, so the detail showed "Region —" until the periodic scope-backfill
poll ran. The drain now resolves `(subscription_id, resource_group, cluster_name)`
— preferring the SB config routing, else discovering the single ElasticBLAST
cluster in the dashboard's subscription (cached ARM call) — and stamps it +
the region durably on the row. Multi-cluster subscriptions fall through to the
existing scope-backfill (we cannot know which cluster the job ran on).

## 3. Query identity (length + molecule)

A new `query_meta_from_fasta` derives the total residue length, record count, and
molecule type (nucleotide / protein heuristic) from the submitted FASTA. The
drain stamps it durably; the detail shows "Query length" + "Molecule" instead of
"—" without a Storage blob read.

## 4. BLAST command preview + raw parameters

The detail grid renders a compact, copy-friendly **BLAST command** line built
from the captured options (the most compact way to see every parameter at once)
and a collapsible **Raw parameters** panel showing the full `config_snapshot`
JSON.

## Self-critique + hardening

* **Task-1 re-fetch on every detail load** → cached in OPS Redis (once per TTL),
  gated to completed jobs, best-effort.
* **Drain ARM calls (cluster discovery + region)** → both cached (60 s / 1 h),
  run in the worker off the request path, only when the SB routing is blank.
* **molecule heuristic** → capped scan, 0.9 nucleotide threshold, degrades to
  "" (unknown) on an ambiguous/empty sequence.
* **Contract** → all new summary fields optional/nullable; 256 backend + 11
  frontend tests green, no regressions.

## Hardening (second pass — 10-point critique, 5 rounds)

A follow-up design critique surfaced one **High** liveness defect and four
robustness gaps; all five were fixed (commit after the feature commit):

1. **(High) Sibling-stats cache defeated when the sibling lacks `db_version`.**
   The live-fetch gate was `not db_version`, so a completed job whose sibling
   reports no `db_version` (or whose cluster is Stopped, so `get_job` times out)
   never filled the positive cache and re-paid the 10 s fetch on **every** detail
   open. Fixed: gate the fetch on a cache **miss**, and write a short-lived
   negative marker (`_attempted`, 5 min TTL) on an empty/raised fetch so the
   re-fetch is bounded yet recovers quickly. Verified live: detail load 1 = 0.94 s
   (fetch + cache), load 2 = 0.06 s (cache hit).
2. **Query length over-counted.** Now counts only alphabetic residues — gaps
   `-`, stops `*`, digits and interior whitespace are excluded.
3. **Molecule mis-call on a tiny sequence.** A minimum-scan guard keeps a
   <4-residue stub as `""` (unknown) rather than a confident wrong call.
4. **BLAST command `-outfmt` could duplicate** when `extra` already carried one;
   the preview now de-duplicates.
5. **Tests** lock in the cache markers (positive vs negative TTL), the query-meta
   length/min-scan edges, and the command de-dup.

Drain ARM calls (cluster discovery + region) were confirmed already cached
(60 s / 1 h, size-bounded), so a 500–1000-message drain burst cannot ARM-throttle.

## API / IaC diff summary

* New: `api/services/blast/external_query_meta.py`,
  `api/services/blast/external_config.py` (sibling-stats remember/recall added),
  `api/tests/test_external_query_meta.py`.
* Changed: `api/tasks/servicebus/tasks.py` (cluster context + query meta stamp),
  `api/routes/blast/jobs.py` (detail sibling-stats merge),
  `api/services/blast/external_jobs.py` (query_meta recover),
  `api/services/blast/external_job_projection.py` (query_length/molecule surface),
  `api/services/blast/job_state.py` (detail query identity),
  `web/src/api/blast.types.ts`,
  `web/src/pages/blastResults/configFormat.ts` (BLAST command builder),
  `web/src/pages/blastResults/BlastJobDetailsGrid.tsx`.
* No IaC / Container App template change.

## Validation evidence

* `uv run pytest -q api/tests/test_external_query_meta.py
  api/tests/test_external_config.py api/tests/test_blast_jobs_routes.py
  api/tests/test_external_job_projection.py api/tests/test_local_to_blast_job.py
  api/tests/test_servicebus_tasks.py api/tests/test_external_blast_api.py` — all
  green (292 across the suites).
* `uv run ruff check` — clean.
* `cd web && npx vitest run src/pages/blastResults/configFormat.test.ts` — 11
  passed; `npm run build` green; eslint clean.
