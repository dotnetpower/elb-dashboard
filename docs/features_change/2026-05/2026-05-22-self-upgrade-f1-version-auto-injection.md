# F1 — `__version__` auto-injection (2026-05-22)

## Motivation

The upgrade reconciler relies on `api.__version__ == row.target_version`
to mark `rolling_out → succeeded` once the new revision boots. Before
this change, `api/__init__.py` hard-coded `__version__ = "0.0.1"` and
ignored every release bump. Result: every self-upgrade would have
stayed in `rolling_out` until the 15-minute stuck guard timed it out
into `failed_rollout`, even when the new revision was perfectly
healthy.

## Change

* `api/__init__.py` — `__version__` is now derived via a fallback chain:
  1. `APP_VERSION` env (set by `api/Dockerfile` `ARG`/`ENV` at build
     time);
  2. `importlib.metadata.version("elb-dashboard")` (set when the
     package is pip-installed);
  3. `pyproject.toml` `project.version` (developer source-mount mode);
  4. `"0.0.0+unknown"` literal fallback.
  Discovery is exception-safe: any source that raises is treated as
  "no value" and we move on.
* `api/Dockerfile` (runtime stage) — adds `ARG APP_VERSION` /
  `APP_GIT_COMMIT` / `APP_BUILD_TIME` and forwards them into `ENV` so
  `api/__init__.py::_from_env` finds them at process start.
* `scripts/dev/postprovision.sh` (`build_image`) — passes
  `APP_VERSION` / `APP_GIT_COMMIT` / `APP_BUILD_TIME` build-args to
  the `elb-api` build (the `elb-frontend` build already did).
* `api/services/upgrade/image_builder.py` (`_argv_for`) — every
  `az acr build` invocation issued from the in-app self-upgrade flow
  now carries `--build-arg APP_VERSION=<target>`. Without this, a
  self-built image would inherit `"0.0.0+unknown"` and the reconciler
  would never observe a matching `__version__`.

## Tests

* `api/tests/test_version.py` (new) — covers env override, pyproject
  fallback, the safety of every discovery helper, and the
  non-empty/string invariant on the module constant.
* `api/tests/test_upgrade_image_builder.py` — asserts the build argv
  contains `APP_VERSION=<target>`.

## Validation

* `uv run ruff check api/__init__.py api/tests/test_version.py api/services/upgrade/image_builder.py api/tests/test_upgrade_image_builder.py` — clean.
* `uv run pytest -q api/tests` — 1176 passed (vs prior 1172).
