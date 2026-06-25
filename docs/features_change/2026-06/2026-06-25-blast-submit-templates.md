---
title: Self-service BLAST submit templates
description: Per-user saved presets of submit-option fields (program, database, algorithm params, sharding) that a researcher can save and re-apply from the submit form, without re-entering the query.
tags:
  - user-guide
  - blast
---

# Self-service BLAST submit templates

## Motivation

Operations-readiness checklist section 4: "self-service submit templates so a
researcher submits directly". The form had hard-coded algorithm presets
(Quick/Standard/Thorough/Publication) and a one-shot "duplicate this job" handoff,
but **no way to save a personal named preset** of submit options for reuse across
sessions. Researchers re-filled the whole parameter set each run.

## User-facing change

* A new **Submit templates** control at the top of the BLAST submit form lets a
  researcher **save the current parameters as a named template** and **apply a
  saved template** from a dropdown (with delete).
* Templates store **option fields only** — program, database, e-value,
  max_target_seqs, outfmt, word size, gap costs, masking, taxonomy filter,
  sharding mode, warmup — **never the query data or job title**. Applying a
  template fills the parameters and leaves the researcher's query untouched.
* Templates are **per-user** and persist across sessions.

## Design

* Storage: one Azure Table row per template under a per-owner partition
  (`PartitionKey = "tmpl:" + owner_oid`, `RowKey = uuid`), mirroring the
  notification-marker / autostop table pattern. The `fields` blob is the same
  `ExportableFormFields` shape the existing config export/duplicate flow uses, so
  the frontend reuses `pickExportableForm` to save and `setForm(...spread)` to
  apply.
* The backend treats `fields` as **opaque** (it does not interpret submit-option
  semantics) and only enforces limits: per-user count (50), name length (120),
  field byte size (32 KB — so the large query FASTA can never be pinned in a
  template) and field key count (200).
* The authenticated `caller.object_id` is the only owner ever passed to the
  service — a caller can read/write only their own partition.

### Hardening (post-critique)

* Duplicate template names are rejected (400).
* Control characters are stripped from names before storage.
* `template_id` path params are pattern-validated (`^[A-Za-z0-9_-]+$` → 422 on
  junk).
* `list` degrades to an empty list on storage fault; create/update/delete surface
  failures so the user knows the save did not land.

> Known minor edge (Low): the per-user count cap is checked then written
> non-atomically, so two truly-simultaneous creates could leave 51 rows. Beat-free
> user-driven path; acceptable.

## API / IaC diff summary

* New backend: `api/services/blast/submit_templates.py` (Table CRUD),
  `api/routes/blast/templates.py` (`GET/POST/PUT/DELETE /api/blast/templates`,
  all `require_caller`), registered in `api/routes/blast/__init__.py`.
* New frontend: `web/src/api/blastTemplates.ts` (+ barrel export),
  `web/src/hooks/useBlastTemplates.ts`,
  `web/src/pages/blastSubmit/BlastTemplatesControl.tsx`, wired into
  `web/src/pages/BlastSubmit.tsx`.
* No IaC change: the `blasttemplates` table is created on first use via
  `create_table_if_not_exists`. No new env var, no new Azure resource, no SAS.

## Validation evidence

* `uv run pytest -q api/tests/test_blast_templates.py` — 17 passed (CRUD,
  partition isolation, count/size/key-count caps, duplicate-name reject, control
  -char strip, route contracts incl. 404/422/400).
* `uv run ruff check api` — all checks passed.
* `cd web && npm run build` — built successfully, no type errors.
* `uv run pytest -q api/tests` — 4604 passed, 3 skipped, 1 failed. The single
  failure (`test_control_plane_env.py::test_bicep_references_every_guard_key`,
  `STORAGE_DATE_LAYOUT_ENABLED`) is pre-existing and unrelated — this change
  touches no `infra/` file.
