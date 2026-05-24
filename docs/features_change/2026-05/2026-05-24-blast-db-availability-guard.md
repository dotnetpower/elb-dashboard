# BLAST Database Availability Guard

## Motivation

BLAST submits could reach the `elastic-blast submit` step even when the selected database prefix was not present in the workload Storage `blast-db` container. The run then failed late in the Submit Job timeline with a raw ElasticBLAST "database not found" error.

## User-facing change

Submit pre-flight now checks that the selected BLAST database has usable files in Storage. The worker repeats the same check during Prepare Run, before any ElasticBLAST submit command is streamed, and fails the job with a clear `database_unavailable` phase when the database is missing.

## API/IaC diff summary

- Added Storage-backed BLAST database availability validation for submit pre-flight and the Celery submit task.
- Added timeline mapping for `database_unavailable` so the failure belongs to Prepare Run instead of Submit Job.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_database_availability.py api/tests/test_blast_tasks.py -k 'database_availability or helpers_are_reexported'`
- `cd web && npm run test -- src/components/BlastStepTimeline/stepState.test.ts`
- `cd web && npm run build`