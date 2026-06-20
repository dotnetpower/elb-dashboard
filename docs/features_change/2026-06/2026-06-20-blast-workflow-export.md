---
title: BLAST workflow-manager export (Nextflow/Snakemake/CWL/WDL)
description: New owner-scoped endpoint that downloads a self-contained workflow module re-submitting a job's exact BLAST parameters.
tags:
  - blast
  - user-guide
---

# BLAST workflow-manager export

## Motivation

Roadmap R3 (issue #57): publication-grade research lives in Nextflow / Snakemake /
CWL / WDL pipelines, not a single web UI. Until now a researcher who wanted to slot a
dashboard BLAST job into a pipeline had to fall back to the `elastic-blast` CLI on a
personal machine. This ships the **backend slice** of R3: a download endpoint that emits
a self-contained workflow module pinned to a source job's exact parameters.

## User-facing change

New endpoint: `GET /api/blast/jobs/{job_id}/export?format=nextflow|snakemake|cwl|wdl`
(default `nextflow`). Owner-scoped and read-only; returns the module as a downloadable
file (`Content-Disposition: attachment`).

Each module:

- Pins the source job's `program`, `db`, and recognised `options` (`evalue`, `word_size`,
  `max_target_seqs`, `dust`, `sharding_mode`, `db_effective_search_space`) plus any
  taxonomy `taxid` / `is_inclusive`.
- Takes the **query FASTA as the only runtime input**, so a pipeline can fan out over many
  queries against the same pinned search configuration.
- Makes one HTTPS `POST` to `{ELB_BASE_URL}/api/blast/jobs` (the inline-FASTA path) using a
  stdlib-only Python snippet — no `jq` / `curl` dependency, correct JSON escaping.

### Safety invariants

- **No secrets in the file.** The bearer token and base URL are read from the environment
  (`ELB_TOKEN`, `ELB_BASE_URL`) at run time; nothing is embedded. No storage URL / SAS.
- **Idempotency-safe.** The source job's `idempotency_key` / `external_correlation_id` are
  intentionally **not** pinned, so each pipeline run creates a fresh job instead of
  collapsing onto the source job's id.

## API / IaC diff summary

- New service `api/services/blast/workflow_export.py` (`build_pinned_request`,
  `render_workflow_export`, `WorkflowExport`, format renderers). Side-effect-free, stdlib only.
- New route `blast_job_export` in `api/routes/blast/jobs_detail.py`
  (`GET /api/blast/jobs/{job_id}/export`), mirroring the citation route's
  `require_caller` + owner check. A job with no recorded database returns `422`
  `export_unavailable`.
- No IaC change.

## Deferred (tracked in #57)

- Frontend **Export** menu on the job detail page (collides with in-flight `blastResults/`
  work; will land separately).
- Auto-generated `elb` PyPI CLI from the OpenAPI spec.
- CWL/WDL are emitted as minimal, valid modules; deeper schema validation against
  `cwltool` / `miniwdl` in CI is a follow-up.

## Validation

- `uv run pytest -q api/tests/test_blast_workflow_export.py` → 21 passed (service unit +
  route tests: pinned params, no-secret/idempotency invariants, per-format markers,
  owner/404/422 paths).
- Regression sweep `api/tests/test_blast_jobs_routes.py test_blast_results_routes.py
  test_route_contracts.py test_blast_citation.py` → 73 passed.
- `uv run ruff check` clean on the touched files.
