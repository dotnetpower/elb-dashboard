---
title: Show db / queries / resource-group in the Configure INI preview for external jobs
description: The Configure step's elastic-blast.ini preview rendered blank db, queries, and azure-resource-group lines for OpenAPI / Service Bus jobs because it only read top-level payload keys; it now resolves them from the canonical_request snapshot and external sync record.
tags:
  - operate
  - blast
  - ui
---

# Show db / queries / resource-group in the Configure INI preview

## Motivation
On a completed BLAST job's **Configure — Generate INI config** step, the rendered
`elastic-blast.ini` preview showed blank values for `azure-resource-group =`,
`[blast] db =`, and `queries =` (and fell back to the per-job cluster name),
even though the job ran successfully against `core_nt` with a real query.

## Root cause
`_config_preview_from_payload` ([api/services/blast/job_state.py](../../../api/services/blast/job_state.py))
read the run identity only from the **top-level** payload keys
(`resource_group`, `db` / `database`, `query_file` / `query_blob_url`,
`cluster_name`). Externally-submitted jobs (sibling OpenAPI / Service Bus →
Table sync) do not carry those top-level keys — the identity lives under
`payload["canonical_request"]` (the canonical submit snapshot) and
`payload["external"]`, and the inline FASTA is uploaded to
`queries/<openapi_id>.fa` with nothing recorded back on the row. So every field
sourced from the missing top-level keys rendered blank. (The same external-job
shape is why [provenance.py](../../../api/services/blast/provenance.py) and the
query preview already use a broader fallback chain.)

## Fix
`_config_preview_from_payload` now resolves each field through the same fallback
chain those projections use:

* **database** ← `canonical_request.database` → top-level `database`/`db` →
  `external.db_name` → `external.db`.
* **resource_group** ← `canonical_request.resource_group` → top-level → `external`.
* **cluster_name** ← `canonical_request.cluster_name`/`aks_cluster_name` →
  top-level → `external`.
* **queries** ← top-level `query_file`/`query_blob_url` → `external.query_url` →
  reconstructed `queries/<external.job_id>.fa` (mirrors `_job_query_blob_path`).

When `canonical_request` is absent the snapshot is rebuilt with
`canonical_submit_snapshot(payload)`, so dashboard-submitted jobs keep using
their top-level keys unchanged.

## User-facing change
The Configure INI preview now shows the real `db`, `queries`,
`azure-resource-group`, and cluster `name` for both dashboard and external
(OpenAPI / Service Bus) jobs instead of blank lines.

## Validation
* `uv run ruff check api` clean; `uv run pytest -q api/tests` → 4139 passed
  (incl. new `test_config_preview_resolves_external_job_identity` and
  `test_config_preview_prefers_top_level_dashboard_keys`).
* Local capture confirmed the external payload resolves
  `db=…/blast-db/core_nt/core_nt`, `queries=<openapi_id>.fa`,
  `resource_group=rg-elb-cluster`, `cluster_name=elb-cluster-01`.
