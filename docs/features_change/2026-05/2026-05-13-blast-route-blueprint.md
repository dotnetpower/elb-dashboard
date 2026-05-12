# BLAST Route Blueprint Extraction

**Date**: 2026-05-13

## Motivation

`api/function_app.py` still owned every `blast/*` HTTP route after the earlier SRP passes. A previous partial extraction of only result routes caused the Azure Function App to fail at startup, so the retry keeps the entire BLAST route family registered from one Blueprint.

## User-facing Change

- No endpoint contract changes.
- All existing `/api/blast/*` routes remain available behind the same bearer-token authentication gate.
- Production package deployment now uses the all-BLAST Blueprint layout instead of the failed partial route extraction package.

## API / IaC Diff

- `api/routes/blast.py` — new Blueprint containing all 28 `blast/*` HTTP handlers and BLAST route helpers.
- `api/function_app.py` — registers the BLAST Blueprint and keeps Durable orchestrators, activities, and entities in the main app module.
- No IaC changes.

## Validation

- Route inventory: 104 functions, 70 HTTP routes, 28 BLAST routes.
- `pytest -q api/tests`: 13 passed.
- `ruff check api/function_app.py api/routes/blast.py`: all checks passed.
- Local smoke: `/api/health` returned 200 and `/api/blast/cost-estimate` returned 200.
- Production smoke: direct Function App and Static Web Apps `/api/health` returned 200 after deploying `funcapp-srp-blast-20260513.zip`.
- Production auth gate: unauthenticated `/api/blast/cost-estimate` and `/api/blast/jobs/{job_id}/results/export` returned 401.
