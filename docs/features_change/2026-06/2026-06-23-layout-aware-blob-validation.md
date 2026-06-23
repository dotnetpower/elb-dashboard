---
title: Layout-aware result-blob ownership validation (flat + dated) — issue #70
description: validate_result_blob_name now accepts a job's result blobs in both the flat and date-tiered layouts while staying as tight as the legacy check (date-prefix-restricted), so dated jobs are not falsely rejected and cross-job access stays blocked.
tags:
  - storage
  - security
---

# Layout-aware result-blob ownership validation

Epic #64, issue #70. Closes the validation gap deferred from #66/#67.

## Motivation

The result-blob ownership guards hardcoded `blob_name.startswith(f"{job_id}/")`.
Under the #67 date-tiered layout a result blob is `YYYY/MM/DD/{job_id}/file`,
which does **not** start with `{job_id}/` — so with `STORAGE_DATE_LAYOUT_ENABLED`
on, every result read/download for a dated job would be falsely rejected. This
makes the validation **layout-aware** without loosening cross-job isolation.

## User-facing change

None for flat jobs (behaviour identical). For dated jobs (flag on) the Results
page reads/downloads/analytics no longer 400 on the ownership check.

## What landed

- `api/services/blast/result_analytics.py`:
  - new `blob_belongs_to_job(blob_name, job_id) -> bool` — the `{job_id}` must be
    a path **segment** (not a substring) with a non-empty file component after
    it and no empty segments, AND the segments **before** it must be empty (flat)
    or exactly a `YYYY/MM/DD` date path. Job ids are unique, so `.../{job_id}/...`
    can only be this job's directory.
  - `validate_result_blob_name` rewritten to run the traversal / URL-encoding /
    leading-slash guards first, then delegate ownership to `blob_belongs_to_job`.
- `api/routes/blast/results.py` — the `file_id` download guard uses
  `blob_belongs_to_job` instead of the flat `startswith` so dated downloads pass.

## Why no feature flag

This is not a *new* positive restriction (§12a Rule 4 default-OFF gate does not
apply): for flat blobs the new check accepts/rejects exactly the same set as the
legacy `startswith({job_id}/)` (e.g. `other/{job_id}/x` was rejected before and
still is — the date-prefix regex keeps the head tight). It only *additionally*
accepts genuine dated blobs, which can only exist once #67's flag is on. So it is
safe unconditionally.

## Security notes (§12)

- Traversal (`..`), backslash, `?`/`#`, `%2e`/`%2f`, and leading-slash are all
  rejected before the ownership check (unchanged).
- The date-prefix allowance is exactly `^\d{4}/\d{2}/\d{2}$`; a 2- or 4-segment
  head, or any non-date head (`other/`, `evil/2026/06/23/`), is rejected — so an
  arbitrary prefix cannot smuggle a blob into a job's namespace.
- No SAS, no `publicNetworkAccess` change. `_validate_blob_path` already rejects
  traversal and is layout-agnostic (no change needed).
- Complements the #69 delete guard (`expected_leaf=job_id` on the directory);
  this guards files, that guards the directory.

## Validation evidence

- `uv run pytest api/tests/test_result_blob_validation.py` → **25 passed** (flat
  + dated accept; cross-job, partial/extra-date head, non-date prefix, bare dir,
  trailing slash, empty segment, substring, traversal, encoding, leading slash
  all rejected).
- `uv run pytest api/tests/test_blast_results_routes.py` → **40 passed**
  (alignments / taxonomy / stream / download validation consumers).
- `uv run pytest api/tests/test_persona_matrix.py api/tests/test_route_contracts.py`
  → **58 passed** (§12a Rule 2: security personas intact).
- `uv run ruff check api` → clean.

## Self-critique (design pass)

- **Contract**: signature unchanged; helper additive; consumers
  (`validate_result_blob_for_job`, alignments/taxonomy/stream/download) all pass. ✓
- **Behaviour delta**: flat identical; only adds dated acceptance. ✓
- **Security**: tightness preserved by the date-prefix regex; traversal guards
  first; persona matrix green. ✓
- Verdict: no Critical/High.
