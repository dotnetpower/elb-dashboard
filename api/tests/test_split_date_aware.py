"""Tests proving split result paths are date-aware (#75).

Responsibility: With the date layout flag on, a split parent's result path-keys
(merged result / report / manifest) resolve to the dated prefix that matches the
parent's stored ``results_prefix``, while split children (created without a dated
stamp) stay flat and self-consistent. With the flag off everything is flat
(byte-identical to the legacy behaviour).
Edit boundaries: Path-key construction behaviour only. No real Azure network —
the state repo is mocked.
Key entry points: ``test_parent_paths_*``, ``test_child_paths_flat``.
Risky contracts: the merge WRITE, the readiness probe, and the Results-page read
all derive from these builders, so a dated parent + flat child here proves the
whole split pipeline is consistent under the flag.
Validation: ``uv run pytest -q api/tests/test_split_date_aware.py``.
"""

from __future__ import annotations

import pytest
from api.services.state.job_state import JobState
from api.tasks.blast import split_pipeline


def _repo_with(rows: dict[str, JobState]):
    class _Repo:
        def get(self, job_id: str) -> JobState | None:
            return rows.get(job_id)

    return _Repo()


def test_parent_paths_flat_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DATE_LAYOUT_ENABLED", raising=False)
    paths = split_pipeline._parent_split_result_paths("parent-1")
    assert paths["merged_result_path"].startswith("parent-1/")
    assert paths["manifest_path"].startswith("parent-1/")


def test_parent_paths_dated_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    dated = JobState(
        job_id="parent-1",
        type="blast",
        status="completed",
        results_prefix="2026/06/23/parent-1/",
    )
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with({"parent-1": dated})
    )
    paths = split_pipeline._parent_split_result_paths("parent-1")
    assert paths["merged_result_path"].startswith("2026/06/23/parent-1/")
    assert paths["merge_report_path"].startswith("2026/06/23/parent-1/")
    assert paths["manifest_path"].startswith("2026/06/23/parent-1/")


def test_child_paths_flat_even_with_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Children are created without a dated stamp → their rows resolve flat, so
    # the merge reads them at {child}/... consistently.
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    flat_child = JobState(job_id="parent-1-qg1", type="blast-child", status="completed")
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: _repo_with({"parent-1-qg1": flat_child}),
    )
    paths = split_pipeline._split_child_result_paths("parent-1-qg1")
    assert paths["merged_result_path"].startswith("parent-1-qg1/")
    assert paths["merge_report_path"].startswith("parent-1-qg1/")


def test_parent_paths_degrade_flat_on_missing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag on but the row lookup returns None → degrade to flat (never raise).
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _repo_with({}))
    paths = split_pipeline._parent_split_result_paths("parent-9")
    assert paths["merged_result_path"].startswith("parent-9/")
