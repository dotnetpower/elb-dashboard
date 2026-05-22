# cancel — raise the child-limit cap and reject overflow explicitly

## Motivation
`cancel_task` hard-coded `repo.list_children(limit=1000)` which silently
truncates if a split parent ever lands more than 1000 children — the
extra children stay running in K8s as orphans even after the dashboard
shows "cancelled".

## User-facing change
None for normal jobs. A pathological split parent with >= 10000
children now fails fast with `cancel_too_many_children` so the operator
sees the partial-cancel risk in the row's error state and can clean
up manually.

## API / IaC diff
* `api/tasks/blast/cancel_task.py::cancel`
  * `child_cap = 10_000` constant.
  * Raises `RuntimeError` with `error_code="cancel_too_many_children"`
    through `_retry_or_fail` (so the dashboard surfaces it) when
    `len(children) >= child_cap`.
* `api/tests/test_blast_tasks.py::test_cancel_split_parent_cascades_to_children`
  * `FakeRepo.list_children` now asserts the new cap.

## Validation
* `uv run pytest -q api/tests/test_blast_tasks.py` — 120 passed.
* `uv run ruff check api/tasks/blast/cancel_task.py` — clean.
