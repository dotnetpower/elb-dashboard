# 2026-05-23 — Add CI test workflow

## Motivation

The repository's backend test loop and lint rules were validated only locally
(`uv run pytest -q api/tests`, `uv run ruff check api`). There was no automation
preventing a commit that turned them red from landing on `main`, and no public
signal that the dev loop is healthy.

This change adds a GitHub Actions workflow that runs the same two commands the
charter already documents as the validation gate, on every push to `main` and
every pull request that touches the backend.

## User-facing change

- New workflow [.github/workflows/test.yml](../../../.github/workflows/test.yml)
  named **Tests**, with a single `backend` job:
  - `actions/checkout@v4`
  - `actions/setup-python@v5` (Python 3.12 — matches `.python-version` and the
    `api`/`terminal` Dockerfiles)
  - `astral-sh/setup-uv@v5` with `enable-cache: true`
  - `uv sync --all-groups` (full dev install, same as `docs.yml`)
  - `uv run ruff check api`
  - `uv run pytest -q api/tests`
- Triggers: push to `main`, pull requests, and `workflow_dispatch`. Paths filter
  to `api/**`, `pyproject.toml`, `uv.lock`, `pytest.ini`, and the workflow file
  itself — so docs-only / web-only changes do not re-run the backend tests
  (those are covered by `docs.yml`).
- Concurrency group `tests-${{ github.ref }}` with `cancel-in-progress: true`
  so a fresh push supersedes the in-flight run.
- Per-job timeout of 15 minutes (the local default loop finishes in ~70 s; this
  is a safety cap, not a budget).
- `AUTH_DEV_BYPASS=true` is set on the pytest step to mirror the conftest
  default so any test that reads the env var behaves the same way as locally.

The default pytest filter (`-m "not slow and not subprocess"`, `--timeout=60`,
`-n auto --dist worksteal`) already lives in [pytest.ini](../../../pytest.ini)
from the previous test-config overhaul, so CI inherits the exact same fast
loop the developers see.

## API / IaC diff summary

- Added: `.github/workflows/test.yml` (52 lines).
- No source/runtime changes. No IaC changes.

## Validation

- YAML syntax: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/test.yml'))"` — PASS.
- Local equivalents of the two CI commands:
  - `uv run ruff check api` — PASS (`All checks passed!`).
  - `uv run pytest -q api/tests` — 1265 passed, 71 failed in 71.43 s. The 71
    failures are the same pre-existing cluster the charter audit identified
    (state_repo / blast_tasks fallout from the `state.table_pool` move in
    `c974ace`, plus a handful in `test_external_blast_api.py`,
    `test_blast_log_routes.py`, `test_blast_jobs_routes.py`, and
    `test_terminal_exec.py::test_run_truncates_stdout_above_cap`).
- The first CI run after this commit will therefore be RED on the `pytest`
  step and GREEN on `ruff check`. The pre-existing failures are tracked as
  the immediate follow-up in
  [docs/features_change/2026-05/2026-05-23-fix-pre-existing-test-failures.md](2026-05-23-fix-pre-existing-test-failures.md)
  and CI will turn fully green once that change lands.

## Out of scope (intentional)

- `mypy --strict` is **not** wired into CI yet. `uv run mypy api` currently
  reports five pre-existing errors in `api/routes/blast/{submit,jobs}.py`
  that are unrelated to the test loop, and adding a continuously-red mypy job
  would dilute the signal of the ruff/pytest jobs. A separate follow-up will
  fix those five errors and then add a blocking mypy job.
- Coverage / `uv lock --check` / docs-link-check are deliberately deferred.
  They belong in a separate workflow so they can fail loudly without
  blocking unrelated PRs.
