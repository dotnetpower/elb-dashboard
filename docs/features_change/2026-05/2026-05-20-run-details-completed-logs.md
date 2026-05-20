# Run Details Completed Logs

## Motivation

The Run details tab showed submit console output while a BLAST job was running, but completed jobs could collapse to a summary-only view because historical logs were stored under `last_output` rather than `output`.

## User-facing Change

- Completed BLAST jobs now show the preserved submit console output when the Submit Job step is expanded.
- New successful submits persist the final submit log on the `submitting` step as completed output.
- Existing jobs that only have `last_output` still render their submit log in Run details.

## API/IaC Diff Summary

- Backend task state now retains final submit stdout/stderr and stream line count on the `submitting` step.
- Frontend Run details now falls back to `last_output` for completed submit logs and can display completion-step output.
- No infrastructure change.

## Validation Evidence

- `uv run pytest -q api/tests/test_blast_tasks.py -k 'merge_progress_payload_keeps_submit_context_and_live_output or merge_progress_payload_keeps_completed_submit_output'`: passed.
- `uv run ruff check api/tasks/blast/__init__.py api/tests/test_blast_tasks.py`: passed.
- `npm run build` in `web/`: passed.
- Browser check on completed job `6ee67a1b-efe7-4c7f-a613-de6714e4b5fb`: expanding Submit Job shows `Submitted successfully` and `Console Output` with preserved azcopy/ElasticBLAST lines.