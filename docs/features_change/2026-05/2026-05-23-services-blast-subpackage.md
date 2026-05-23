# 2026-05-23 — services/blast subpackage (15 modules)

## Motivation
Final batch of Phase A. Move all remaining `blast_*.py` flat files into the
existing `services/blast/` subpackage. After this commit, `api/services/` no
longer holds any `<prefix>_<name>.py` business-logic files — everything is in
a domain subpackage with a compatibility shim left at the legacy path.

## Diff
- 15 files moved: compatibility, config, db_metadata, equivalence_evidence,
  events, external_jobs, job_state, oracles, provenance, queue,
  result_analytics, result_artifacts, result_manifest, results_parser,
  submit_payload — all `api/services/blast_<X>.py` → `api/services/blast/<X>.py`.
- Internal cross-imports inside the moved files rewritten to the sibling path.
- 15 shims at legacy flat paths use the same module-level `__getattr__` proxy
  pattern adopted in the k8s batch — avoids hand-maintaining `__all__` lists
  with hundreds of symbols.
- All in-repo callers (services, routes, tasks, tests) + monkey-patch strings
  updated.

## Validation
- `uv run pytest -q api/tests` → 1260 passed in 61.77s
- `uv run ruff check api` → All checks passed
