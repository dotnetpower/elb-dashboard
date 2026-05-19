# Job file previews

## Motivation

The BLAST job detail timeline could mark Upload Query and Configure as complete but still show `Could not load input.fa` or `Could not load elastic-blast.ini`. The preview route only knew one query blob path and treated `elastic-blast.ini` as a result file, while the submit task passed the generated config to the terminal sidecar without persisting a preview copy.

## User-facing change

The job detail page now previews the uploaded query from the correct `queries` blob path, including the dashboard upload fallback path. The Configure step also previews `elastic-blast.ini`; new jobs persist a copy under `queries/<job-id>/elastic-blast.ini`, and existing jobs can regenerate the config preview from the saved job payload when the blob is absent.

## API/IaC diff summary

- `/api/blast/jobs/{job_id}/file` now resolves query previews through safe in-job candidate paths and reads `elastic-blast.ini` from the `queries` container.
- The file route falls back to generating an INI preview from the stored job payload if the config blob does not exist.
- `api.tasks.blast.submit` uploads the generated config to the `queries` container as a preview artifact before invoking `elastic-blast submit`.
- The React file preview cache key now includes storage context, and the timeline passes explicit query/config blob names.

## Validation evidence

- `uv run pytest -q api/tests/test_smoke.py -k "blast_job_file"`
- `uv run ruff check api/routes/stubs.py api/tasks/blast.py api/tests/test_smoke.py`
