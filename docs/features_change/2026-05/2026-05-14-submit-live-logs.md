# Execution Step Live Logs

## Motivation

The BLAST results page showed long-running steps with generic messages. The worst case was `Submit Job`, where the backend ran `elastic-blast submit` inside a blocking activity, so Durable custom status could not expose stdout/stderr until the activity returned. Other steps also had useful metadata but did not render it as operational logs.

## User-facing change

The submit step now starts the AKS submit helper job and then polls it every few seconds. The execution timeline displays the helper job name, poll attempt, pod phase, container state, and recent submit console output while the job is still running. Other steps now show best-effort logs for VM readiness, storage access, query upload, config generation, warmup, BLAST status polling, and result verification.

## API/IaC diff summary

- Added `start_elastic_blast_submit_activity` to create the AKS submit helper job without blocking for completion.
- Added `check_elastic_blast_submit_activity` to read helper job status and tail submit pod logs.
- Updated `submit_blast_orchestrator` to publish `last_output` / `output` into Durable custom status across execution steps.
- Updated the timeline UI to render active and completed step logs where available.
- No infrastructure changes.

## Validation evidence

- `npm run build` passed.
- `python -m py_compile api/activities/blast.py api/function_app.py api/orchestrators/submit_blast.py` passed.
- `pytest -q api/tests/test_models.py api/tests/test_passwords.py api/tests/test_sanitise.py` passed: 13 tests.
- `git diff --check` passed for the changed files.