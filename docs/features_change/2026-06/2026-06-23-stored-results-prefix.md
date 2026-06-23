---
title: Canonical per-job results prefix as system-of-record (decouple from job_id)
description: Persist JobState.results_prefix and route every result-listing call through a single resolver instead of reconstructing {job_id}/ inline, so a future date-tiered layout only changes the stored value.
tags:
  - storage
  - architecture
---

# Canonical per-job results prefix as system-of-record

Epic #64, issue #66.

## Motivation

Seven call sites reconstructed a job's results-container prefix inline as
`f"{job_id}/"` (two of them as a bare `job_id` with **no trailing slash** — a
latent `name_starts_with` prefix-collision bug where `job-abc` also matches
`job-abcd/...`). That hardcoding makes the physical blob layout inseparable from
`job_id`, blocking the date-tiered layout in issue #67. This change makes the
prefix a **stored, resolver-mediated value**.

## User-facing change

None. This is a behaviour-preserving refactor: every stored `results_prefix`
equals the legacy `{job_id}/`, so the resolved prefix is byte-identical to
before — except the two no-slash sites, which are now collision-free (strictly
more correct; elastic-blast always writes under `{job_id}/...` so real data
matches identically).

## What landed

- `api/services/state/job_state.py` — new durable column `JobState.results_prefix`.
  `to_entity` defaults it to `{job_id}/` so every created row carries it;
  `from_entity` reads it back (legacy rows → `None`). `update()` is a MERGE patch
  and never touches it, so it survives status writes.
- `api/services/storage/job_prefix.py` (new) — the single resolver:
  `normalize_results_prefix` (collision-free, single trailing slash, strips `..`),
  `default_results_prefix` (`{job_id}/` fallback), `results_prefix_from_state`
  (honours the stored column), `elastic_blast_subdir_prefix` (`<prefix>job-`).
- Routed all seven reconstruction sites through the resolver:
  `result_analytics` (success-marker + `list_parseable_result_blobs`, now with an
  optional `prefix=` for #67 threading), `result_artifacts.build_result_manifest_payload`
  (optional `prefix=`, no-slash bug fixed), `routes/blast/results.py` listing
  (no-slash bug fixed), `blast/job_state` + `tasks/blast/submit_runtime`
  elastic-blast id discovery, `tasks/blast/split_pipeline` parent/child probes,
  `blast/runtime_failure` failure-detail listing.

The security/path-validation guards that also reference `f"{job_id}/"`
(`routes/blast/results.py` lines 96/109/430, `result_analytics.validate_result_blob_name`)
are intentionally **left for issue #70**, which generalises them against the
stored prefix.

## API / IaC diff summary

- No API route/response shape change — `results_prefix` is internal only
  (`_local_to_blast_job` maps fields explicitly; it is not auto-serialized).
- No IaC change.

## Validation evidence

- `uv run pytest api/tests/test_storage_job_prefix.py` → **20 passed** (resolver
  normalization incl. traversal fallback + JobState round-trip).
- Regression (no behaviour change): `test_storage_data` + `test_blast_results_routes`
  + `test_state_repo` → **104 passed**; `test_blast_result_analytics_organism` +
  `test_blast_result_manifest` + `test_job_artifacts` → **42 passed**;
  `test_blast_jobs_routes` → **24 passed**; `test_local_to_blast_job` → **47 passed**
  (additive column does not leak into the serializer); `test_sharded_merge` +
  `test_sharded_db_profile` → **15 passed** (split-pipeline probes).
- `uv run ruff check api` → clean.
- External jobs (`external_jobs.py` `repo.create`) flow through `to_entity`, so
  they get the `{job_id}/` default that matches the sibling's
  `results/{job_id}/` contract.

## Self-critique (design pass)

- **Contract**: additive optional column; `update()` MERGE preserves it; no API
  surface change (`_local_to_blast_job` is explicit). ✓
- **Behaviour delta**: only the two no-slash → slash fixes, strictly safer
  (latent prefix-collision bug closed). ✓
- **Security**: resolver strips `..` defensively; the authoritative traversal
  guard stays in `validate_result_blob_name` (untouched, deferred to #70). ✓
- **Backward-compat**: legacy rows (no column) resolve via `{job_id}/` fallback;
  new optional `prefix=` kwargs default to None. ✓
- Verdict: no Critical/High.
