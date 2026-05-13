# BLAST DB Update Route Runtime Fix

## Motivation

The dashboard calls `/api/blast/databases/check-updates` on load. Production returned HTTP 500 because the route used `_requests` without importing it.

## User-facing change

The database update check endpoint now returns the latest NCBI BLAST database directory version instead of a runtime `name '_requests' is not defined` error. Related BLAST utility routes that also use `_requests` and `ValidationError` now import those dependencies explicitly.

## API/IaC diff summary

- Added missing `requests as _requests` import in `api/routes/blast_jobs.py`.
- Added missing `requests as _requests` and `ValidationError` imports in `api/routes/blast_tools.py`.
- No IaC changes.

## Validation evidence

- `python -m py_compile api/routes/blast_jobs.py api/routes/blast_tools.py` passed.
- `pytest -q api/tests/test_models.py api/tests/test_passwords.py api/tests/test_sanitise.py` passed: 13 tests.
- `scripts/dev/deploy-api.sh` deployed `funcapp-202605132319.zip` and reported `/api/health` HTTP 200.
- Browser smoke check on the production Static Web App returned HTTP 200 for `/api/blast/databases/check-updates` with `latest_version: 2026-05-09-01-05-02`.
- Dashboard cards render as `OK` / `Ready` after reload.
