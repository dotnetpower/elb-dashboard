---
title: Show full BLAST parameters (outfmt, taxonomy, …) + region on queue/API job details
description: Capture the submitted BLAST options and resolve the AKS region for Service Bus and external-API BLAST jobs so the Run-details view shows outfmt, e-value, word size, taxonomy filter, BLAST/DB version, run time, and the region instead of an empty config_snapshot and "Region —".
tags:
  - blast
  - ui
---

# Full parameter + region capture for queue / API BLAST jobs (2026-06-24)

## Motivation

A Service Bus (queue) or external-API BLAST job showed an empty parameter set on
the Run-details view — `config_snapshot` was `null`, so outfmt / e-value / word
size / taxonomy filter were all absent, and "Region" rendered as "—". Root cause
(confirmed on a live job): the sibling `/v1/jobs` record never echoes the BLAST
options or the region back, and the dashboard's drain row stored only
`program` + `db`. So the submitted options were dropped on the floor.

## User-facing change

The **Run-details** grid now shows, for queue/API jobs too:

* **Output format** — the effective specifier (`7 std staxids sscinames stitle
  qcovs`) when present, else the bare code.
* **E-value, Max targets, Word size, Dust** and any other recorded option.
* **Taxonomy filter** — `include taxid N` / `exclude taxid N`.
* **Region** — resolved from the cluster (no longer "—").
* **BLAST version, DB version, Run time** — from the sibling's own record.
* When a job genuinely has no recorded options, an explicit *"not recorded for
  this job"* hint instead of a blank (distinguishes missing-capture from N/A).

## How the data is captured

The sibling cannot report the options, so the **only** source is the request the
dashboard itself received:

* **SB drain** (`_persist_drain_row_and_trace`) builds a flat `config_snapshot`
  from the submit payload (`options` / `blast_options`) and stamps it — plus the
  resolved region — durably on `payload.external`.
* **Direct API submit** remembers the options in OPS Redis (`remember_config_snapshot`);
  the jobs sync re-attaches them to the durable row on first persist.
* `_sync_external_jobs_to_table` recovers the stored `config_snapshot` + region
  (mirrors the `submission_source` / `queue_origin` relabel pattern).
* The list projection (`_external_to_blast_job`) and the detail projection
  (`_local_to_blast_job`) both surface `config_snapshot`, `infrastructure.region`,
  and the sibling stats (`db_version`, `blast_version`, `run_seconds`).

## Self-critique + hardening rounds (5)

Design rubric findings were hardened in 5 rounds (all tested):

1. **Region cache unbounded** → size-bounded (`_REGION_CACHE_MAX`, evict
   oldest-expiring).
2. **Failed region cached for 1 h** → negative results cached only 60 s so a
   transient AKS/RBAC blip recovers fast.
3. **Taxonomy exclude/include flags dropped** → `taxids` / `negative_taxids`
   added to the snapshot + the label honours them.
4. **Free-form option strings unbounded** → `additional_options` / `extra`
   capped at 1 KiB so a hostile/huge value cannot bloat the row.
5. **ARM region call on the list hot path** → the drain resolves + stamps the
   region durably in the worker; the projection reads it from the row and only
   resolves live (cached) when absent.

## API / IaC diff summary

* New: `api/services/blast/external_config.py` (snapshot builder, cached region
  resolver, remember/recall), `web/src/pages/blastResults/configFormat.ts`.
* Changed (additive summary fields): `api/tasks/servicebus/tasks.py`,
  `api/routes/elastic_blast.py`, `api/services/blast/external_jobs.py`,
  `api/services/blast/external_job_projection.py`,
  `api/services/blast/job_state.py`, `web/src/api/blast.types.ts`,
  `web/src/pages/blastResults/BlastJobDetailsGrid.tsx`.
* No IaC / Container App template change.

## Validation evidence

* `uv run pytest -q api/tests/test_external_config.py` — 12 passed.
* `uv run pytest -q api/tests/test_external_job_projection.py
  api/tests/test_local_to_blast_job.py api/tests/test_blast_jobs_routes.py
  api/tests/test_servicebus_tasks.py` — 160 passed (no regressions from the
  additive summary fields).
* `uv run ruff check` — clean.
* `cd web && npx vitest run src/pages/blastResults/configFormat.test.ts` — 9
  passed; `npm run build` green; eslint clean.
