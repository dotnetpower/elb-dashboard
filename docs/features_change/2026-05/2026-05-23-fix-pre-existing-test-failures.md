# Fix pre-existing test failures so backend CI turns green

## Motivation
After the `state_repo` flat→package refactor (commit `c974ace`) and the
parallel-test default loop (commit `5d9c569`), `uv run pytest -q api/tests`
reported 71 failures on `main`. The newly-added backend CI workflow (commit
`5e52d08`) therefore lit up red on its first push. This change makes the
default loop deterministically green so the workflow blocks regressions
instead of being permanently broken.

## User-facing change
None. Test infrastructure only.

## Diff summary
* `api/tests/test_state_repo.py` — change the two top-of-file imports from
  the legacy facade `api.services.state_repo` to the real module
  `api.services.state.repository`. Every in-test `monkeypatch.setattr` uses
  the local `state_repo` binding, which now points at the module that
  actually owns `TableClient`, `TableServiceClient`, `get_credential`,
  `_ENSURED_TABLES`, `JobStateRepository`, `JobState`, etc. The legacy
  facade does not re-export the SDK symbols, so facade-targeted patches
  silently no-op'd → the eager `AZURE_TABLE_ENDPOINT` constructor check
  raised and 8 tests crashed. No production import paths are touched.
* `pytest.ini` — switch xdist distribution from `worksteal` to `loadfile`.
  `worksteal` steals individual tests across workers mid-session; with
  `JobStateRepository` and friends now backed by per-module singletons
  (`_DEFAULT_REPO` + lock) plus daemon threads from the `TestClient`-driven
  routes, that movement causes ~12 cross-worker pollution failures and an
  intermittent `node down: Not properly terminated` worker crash.
  `loadfile` keeps every test from one file on the same worker, preserving
  file-level invariants while still using `-n auto`. Default-loop runtime
  is unchanged (~21-25 s on this host).

## API / IaC diff
None.

## Validation
* `uv run ruff check api` — clean.
* `uv run pytest -q api/tests` — `1369 passed in 25.41s` (three consecutive
  full runs: 23.90 s / 23.55 s / 24.87 s — all green, no worker crashes).
* `uv run pytest -q api/tests -n 0 -m ''` — `1435 passed in 66.09s`
  (serial, including `slow` + `subprocess` marks).
