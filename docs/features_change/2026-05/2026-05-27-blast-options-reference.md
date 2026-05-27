---
date: 2026-05-27
area: docs
tags:
  - features_change
  - docs
  - blast
---

# BLAST Options Reference (mkdocs)

## Motivation

BLAST execution options were documented across several pages without a single
reference: [docs/user-guide/new-search.md](../../user-guide/new-search.md)
listed only a handful of flag names, [docs/user-guide/api-reference.md](../../user-guide/api-reference.md)
showed example payloads, and the canonical mapping rules (e.g. `low_complexity_filter`
↔ `dust`) lived inside research notes. There was no end-user-facing
catalogue of "every option, its default, its CLI flag, and what blocks
submit".

## User-facing change

- New page **[BLAST Options Reference](../../user-guide/blast-options.md)** under *User Guide*, listing every option exposed by the submit form / OpenAPI / Celery backend with its default, allowed values, CLI mapping, and validation rule.
- Sections cover: program & query, search set, taxonomy filter, task profile, execution profile (cluster + sharding + warmup), algorithm parameters, preflight gates, and OpenAPI-only fields.
- [docs/user-guide/new-search.md](../../user-guide/new-search.md) now links to the new reference at the end of the *Algorithm parameters* paragraph.

## API / IaC diff summary

None — documentation only. No code, schema, or infra changes.

## Validation evidence

- `uv run mkdocs build --strict` — builds clean (only unrelated MkDocs 2.0 promo banner + the expected "new file has no git history" warning for the new page).
- All source-of-truth links in the new page (`api/_http_utils.py`, `api/services/blast/config.py`, `api/services/blast/submit_payload.py`, `api/services/blast/task_config.py`, `api/services/sharding_precision.py`, `api/routes/blast/preflight.py`, `api/routes/blast/submit.py`, `web/src/api/blast.ts`, `web/src/pages/blastSubmitModel.ts`, `web/src/pages/blastSubmit/useSubmitMutation.ts`, `web/src/pages/blastSubmit/AlgorithmParametersSection.tsx`, `web/src/pages/blastSubmit/shardingAvailability.ts`, `web/src/pages/blastSubmit/computeEnvironment.ts`) verified to exist via `[[ -f $f ]]` audit.
