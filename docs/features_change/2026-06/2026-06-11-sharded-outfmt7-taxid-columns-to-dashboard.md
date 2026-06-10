---
title: Sharded outfmt 7 taxid/sciname columns reach the dashboard
description: Preserve the shard # Fields header through the finalizer merge and map blastn's spaced taxonomy column labels so Scientific Name / Taxonomy populate.
tags:
  - blast
  - release
---

# Sharded `outfmt 7` taxid / scientific-name columns reach the dashboard

## Motivation

After the OpenAPI plane started accepting sharded `-outfmt 7 std staxids
sscinames` submits (see
[2026-06-10-openapi-outfmt7-gate-rebuild.md](2026-06-10-openapi-outfmt7-gate-rebuild.md)),
a real `core_nt` run completed and merged correctly — the
`merged_results.out.gz` data rows carried the taxid (`10244`) and scientific
name (`Monkeypox virus`) in their trailing columns. But the dashboard's BLAST
Results view still showed an empty **Scientific Name** column and "Unknown"
review badges.

Two independent root causes, both downstream of the merge:

1. **The finalizer stripped the `# Fields:` header.** When the finalizer
   concatenates each shard's `.out.gz` into `MERGE_INPUT`, upstream uses
   `awk '!/^#/'`, which drops *every* comment line — including the
   authoritative `# Fields:` line that names the extended columns
   (`... bit score, subject tax ids, subject sci names`). With no `# Fields:`
   line reaching `merge-sharded-results.sh`, the merge fell back to the
   standard 12-field header even though the data rows had 14 columns. The
   merged output was therefore self-contradictory: 14-column rows under a
   12-column header.

2. **The parser didn't recognise blastn's spaced taxonomy labels.** BLAST+
   2.17.0 writes the header as `subject tax ids` / `subject sci names` (with
   spaces), but `_FIELD_LABEL_TO_COLUMN` only mapped the run-together
   `subject taxids` form. Unmapped labels fall back to
   `label.replace(" ", "_")`, so the column was named `subject_tax_ids` and the
   dashboard's `hit.staxids` lookup missed it.

## User-facing change

Sharded `outfmt 7` runs that request `staxids` / `sscinames` now populate the
**Scientific Name** column and the **Taxonomy** tab in BLAST Results, and the
review badges classify instead of showing "Unknown".

Note on the other two columns the report mentioned:

- **Description** (subject title, `stitle`) and **HSP Cover** (`qcovs`, derived
  from `qlen`) are *not* part of `7 std staxids sscinames` — `std` is the 12
  core columns plus the two taxonomy columns, with no `stitle` or `qlen`. Those
  cells are correctly blank for that specifier. To populate them, add `stitle`
  and `qlen` (or `qcovs`) to the outfmt, or use `outfmt 5` (XML), which carries
  subject title and query length.

## API / IaC diff summary

- `terminal/patch_elastic_blast.py` — `patch_finalizer_script` now rewrites the
  shard concatenation `awk '!/^#/'` to `awk '/^# Fields:/ || !/^#/'` so the
  Fields header survives while other comment noise is still stripped. Verified
  to apply cleanly at the build ref `7a471297`.
- `api/services/blast/results_parser.py` — `_FIELD_LABEL_TO_COLUMN` gains
  `subject tax ids` / `subject tax id` → `staxids` aliases.
- Tests: `api/tests/test_blast_results_parser.py` (spaced taxonomy header maps
  to `staxids` / `sscinames`), `api/tests/test_terminal_patch_elastic_blast.py`
  (awk filter preserves `# Fields:` while stripping other comments; patch
  wiring guard).

## Rollout

- The parser fix ships in the `elb-api` image via the normal Container App
  deploy.
- The finalizer fix is baked into the `elb-openapi` image (the finalizer +
  merge scripts are delivered to the cluster as a ConfigMap from that image),
  so it requires an `elb-openapi` rebuild + pin bump and pod redeploy, same as
  the gate rebuild. The next `elb-openapi` tag picks it up.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_results_parser.py
  api/tests/test_terminal_patch_elastic_blast.py` — 39 passed.
- Dry-run: applied `patch_elastic_blast.py` against a fresh `7a471297`
  worktree; `elb-finalizer-aks.sh:164` becomes
  `awk '/^# Fields:/ || !/^#/'` with no anchor drift.
- Live (pre-fix) evidence: shard_00 output carried
  `# Fields: ... subject tax ids, subject sci names` (14 cols), but
  `merged_results.out.gz` header listed only the std 12 — confirming the
  finalizer dropped the Fields line.
