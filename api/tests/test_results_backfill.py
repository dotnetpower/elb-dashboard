"""Tests for the results-layout backfill + the dfs rename guard.

Responsibility: Prove ``dfs_io.rename_directory_dfs`` enforces its guards and is
idempotent, and that ``results_backfill.backfill_results_layout`` is a no-op when
the flags are off, plans without touching storage in dry-run, moves flat jobs +
stamps the dated prefix when live, skips already-dated jobs, and never raises per
job.
Edit boundaries: Rename-guard + backfill orchestration only. No real Azure
network — repo, credential, and dfs rename are mocked.
Key entry points: ``test_rename_*``, ``test_backfill_*``.
Risky contracts: a per-job rename MUST pass ``expected_src_leaf=job_id``; the
backfill MUST be gated on BOTH flags and idempotent.
Validation: ``uv run pytest -q api/tests/test_results_backfill.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.state.job_state import JobState
from api.services.storage import dfs_io, results_backfill
from azure.core.exceptions import ResourceNotFoundError


class _FakeDirClient:
    def __init__(self, *, absent: bool = False) -> None:
        self.absent = absent
        self.renamed_to: str | None = None

    def rename_directory(self, *, new_name: str) -> None:
        if self.absent:
            raise ResourceNotFoundError("source not found")
        self.renamed_to = new_name


class _FakeFs:
    def __init__(self, *, absent: bool = False) -> None:
        self.absent = absent
        self.client = _FakeDirClient(absent=absent)

    def get_directory_client(self, _path: str) -> _FakeDirClient:
        return self.client


@pytest.fixture
def _patch_fs(monkeypatch: pytest.MonkeyPatch):
    def _install(fs: _FakeFs) -> _FakeFs:
        monkeypatch.setattr(
            "api.services.storage.dfs_client_pool._dfs_filesystem",
            lambda *_a, **_k: fs,
        )
        return fs

    return _install


# --- rename_directory_dfs guards -------------------------------------------


def test_rename_moves_and_returns_true(_patch_fs) -> None:
    fs = _patch_fs(_FakeFs())
    ok = dfs_io.rename_directory_dfs(
        object(), "elbstg01", "results", "job-1", "2026/06/23/job-1", expected_src_leaf="job-1"
    )
    assert ok is True
    assert fs.client.renamed_to == "results/2026/06/23/job-1"


def test_rename_absent_source_returns_false(_patch_fs) -> None:
    _patch_fs(_FakeFs(absent=True))
    ok = dfs_io.rename_directory_dfs(
        object(), "elbstg01", "results", "job-1", "2026/06/23/job-1", expected_src_leaf="job-1"
    )
    assert ok is False


@pytest.mark.parametrize("bad_src", ["", "/", "a/../b"])
def test_rename_rejects_bad_source(_patch_fs, bad_src: str) -> None:
    _patch_fs(_FakeFs())
    with pytest.raises(ValueError):
        dfs_io.rename_directory_dfs(object(), "elbstg01", "results", bad_src, "x/y")


def test_rename_rejects_bad_dest(_patch_fs) -> None:
    _patch_fs(_FakeFs())
    with pytest.raises(ValueError):
        dfs_io.rename_directory_dfs(object(), "elbstg01", "results", "job-1", "a/../b")


def test_rename_refuses_wrong_src_leaf(_patch_fs) -> None:
    _patch_fs(_FakeFs())
    with pytest.raises(ValueError):
        dfs_io.rename_directory_dfs(
            object(), "elbstg01", "results", "2026/06/23", "x", expected_src_leaf="job-1"
        )


# --- backfill --------------------------------------------------------------


class _FakeRepo:
    def __init__(self, rows: list[JobState]) -> None:
        self.rows = rows
        self.updated: list[dict[str, Any]] = []

    def list_completed(self, *, limit: int = 100) -> list[JobState]:
        return self.rows[:limit]

    def update(self, job_id: str, **kw: Any) -> None:
        self.updated.append({"job_id": job_id, **kw})


def _flat_job(job_id: str = "job-1") -> JobState:
    return JobState(
        job_id=job_id,
        type="blast",
        status="completed",
        storage_account="elbstg01",
        created_at="2026-06-23T09:00:00+00:00",
        results_prefix=f"{job_id}/",
    )


def test_backfill_noop_when_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DFS_ENABLED", raising=False)
    monkeypatch.delenv("STORAGE_DATE_LAYOUT_ENABLED", raising=False)
    out = results_backfill.backfill_results_layout()
    assert out["enabled"] is False
    assert out["scanned"] == 0


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")


def test_backfill_dry_run_plans_without_moving(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    repo = _FakeRepo([_flat_job("job-1")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    out = results_backfill.backfill_results_layout(dry_run=True)
    assert out["enabled"] is True
    assert out["planned"] == 1
    assert out["moved"] == 0
    assert repo.updated == []  # storage + row untouched in dry-run
    assert out["plan"][0]["from"] == "results/job-1"
    assert out["plan"][0]["to"] == "results/2026/06/23/job-1"


def test_backfill_live_moves_and_stamps_row(monkeypatch: pytest.MonkeyPatch, _patch_fs) -> None:
    _enable(monkeypatch)
    repo = _FakeRepo([_flat_job("job-1")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    fs = _patch_fs(_FakeFs())
    out = results_backfill.backfill_results_layout(dry_run=False)
    assert out["moved"] == 1
    assert fs.client.renamed_to == "results/2026/06/23/job-1"
    assert repo.updated == [{"job_id": "job-1", "results_prefix": "2026/06/23/job-1/"}]


def test_backfill_skips_already_dated(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    dated = JobState(
        job_id="job-2",
        type="blast",
        status="completed",
        storage_account="elbstg01",
        created_at="2026-06-20T00:00:00+00:00",
        results_prefix="2026/06/20/job-2/",
    )
    repo = _FakeRepo([dated])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    out = results_backfill.backfill_results_layout(dry_run=False)
    assert out["skipped"] == 1
    assert out["moved"] == 0
    assert repo.updated == []


def test_backfill_records_error_without_raising(monkeypatch: pytest.MonkeyPatch, _patch_fs) -> None:
    _enable(monkeypatch)
    repo = _FakeRepo([_flat_job("job-1")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    _patch_fs(_FakeFs())
    monkeypatch.setattr(
        "api.services.storage.dfs_io.rename_directory_dfs",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dfs down")),
    )
    out = results_backfill.backfill_results_layout(dry_run=False)
    assert out["errors"] == 1
    assert out["moved"] == 0
    assert repo.updated == []  # row not stamped on rename failure
