# BLAST Submit Live Progress

## Motivation

Dashboard BLAST jobs could remain on the Submit Job step without visible progress while `elastic-blast submit` was still running in the terminal sidecar. Cancel requests also missed the AKS context when the browser sent an empty body.

## User-Facing Change

The job detail page can now show live submit output from the terminal exec stream under the Submit Job step. Running job detail refreshes also reconcile terminal AKS completion when Kubernetes already reports the BLAST jobs completed or failed. Cancel requests include the job's subscription, resource group, cluster, and storage context, and the API falls back to the persisted job payload when the browser omits them.

## API/IaC Diff Summary

- `api.tasks.blast.submit` now uses the terminal exec streaming endpoint for `elastic-blast submit` and stores compact progress under the job payload `_progress.steps` field.
- `/api/blast/jobs/{job_id}` exposes the persisted progress as `custom_status.steps` and opportunistically refreshes completed/failed AKS state for running dashboard jobs.
- `/api/blast/jobs/{job_id}/cancel` recovers missing AKS context from the persisted job payload.
- `web/src/api/blast.ts` and `useBlastResultActions` send cancel context from the job detail page.
- No IaC changes.

## Validation Evidence

- `uv run pytest -q api/tests/test_local_to_blast_job.py api/tests/test_blast_tasks.py` — 76 passed.
- `uv run ruff check api/tasks/blast.py api/routes/stubs.py api/tests/test_local_to_blast_job.py api/tests/test_blast_tasks.py` — passed.
- `cd web && npm run build` — passed.
