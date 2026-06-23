"""Tests for the best-effort recursive job-storage purge + the dfs delete guard.

Responsibility: Prove ``dfs_io.delete_directory_dfs`` enforces its safety guards
(non-empty, no ``..``, ``leaf == expected``) and is idempotent (absent dir →
False), and that ``job_purge.purge_job_result_storage`` is a no-op when dfs is
off / the job is external / scope is missing, deletes the job's own result+query
directories when on, and never raises.
Edit boundaries: Delete-guard + purge orchestration only. No real Azure network —
the filesystem + directory clients are fakes.
Key entry points: ``test_delete_directory_*``, ``test_purge_*``.
Risky contracts: a per-job delete MUST pass ``expected_leaf=job_id`` so it can
never target a parent date bucket; the purge MUST never raise into the delete
route.
Validation: ``uv run pytest -q api/tests/test_job_purge.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.state.job_state import JobState
from api.services.storage import dfs_io, job_purge
from azure.core.exceptions import ResourceNotFoundError


class _FakeDirClient:
    def __init__(self, *, absent: bool = False) -> None:
        self.absent = absent
        self.deleted = False

    def delete_directory(self) -> None:
        if self.absent:
            raise ResourceNotFoundError("dir not found")
        self.deleted = True


class _FakeFs:
    def __init__(self, *, absent_dirs: set[str] | None = None) -> None:
        self.absent_dirs = absent_dirs or set()
        self.deleted: list[str] = []

    def get_directory_client(self, path: str) -> _FakeDirClient:
        client = _FakeDirClient(absent=path in self.absent_dirs)
        # Record deletions through a wrapper so the test can assert paths.
        orig = client.delete_directory

        def _record() -> None:
            orig()
            self.deleted.append(path)

        client.delete_directory = _record  # type: ignore[method-assign]
        return client


@pytest.fixture
def _patch_fs(monkeypatch: pytest.MonkeyPatch):
    def _install(fs: _FakeFs) -> _FakeFs:
        monkeypatch.setattr(
            "api.services.storage.dfs_client_pool._dfs_filesystem",
            lambda *_a, **_k: fs,
        )
        return fs

    return _install


# --- delete_directory_dfs guards -------------------------------------------


def test_delete_directory_deletes_and_returns_true(_patch_fs) -> None:
    fs = _patch_fs(_FakeFs())
    assert (
        dfs_io.delete_directory_dfs(
            object(), "elbstg01", "results", "job-1/", expected_leaf="job-1"
        )
        is True
    )
    assert fs.deleted == ["job-1"]


def test_delete_directory_absent_returns_false(_patch_fs) -> None:
    _patch_fs(_FakeFs(absent_dirs={"job-1"}))
    assert (
        dfs_io.delete_directory_dfs(
            object(), "elbstg01", "results", "job-1/", expected_leaf="job-1"
        )
        is False
    )


def test_delete_directory_dated_leaf_ok(_patch_fs) -> None:
    fs = _patch_fs(_FakeFs())
    dfs_io.delete_directory_dfs(
        object(), "elbstg01", "results", "2026/06/23/job-1/", expected_leaf="job-1"
    )
    assert fs.deleted == ["2026/06/23/job-1"]


@pytest.mark.parametrize("bad", ["", "/", "a/../b"])
def test_delete_directory_rejects_invalid_path(_patch_fs, bad: str) -> None:
    _patch_fs(_FakeFs())
    with pytest.raises(ValueError):
        dfs_io.delete_directory_dfs(object(), "elbstg01", "results", bad)


def test_delete_directory_refuses_wrong_leaf(_patch_fs) -> None:
    # A bug that tried to delete a whole day bucket must be refused.
    _patch_fs(_FakeFs())
    with pytest.raises(ValueError):
        dfs_io.delete_directory_dfs(
            object(), "elbstg01", "results", "2026/06/23/", expected_leaf="job-1"
        )


# --- purge_job_result_storage ----------------------------------------------


def _state(**kw: Any) -> JobState:
    base = {"job_id": "job-1", "type": "blast", "status": "deleted", "storage_account": "elbstg01"}
    base.update(kw)
    return JobState(**base)


def test_purge_noop_when_dfs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DFS_ENABLED", raising=False)
    out = job_purge.purge_job_result_storage(_state())
    assert out["purged"] is False
    assert out["reason"] == "dfs_disabled"


def test_purge_skips_external_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    out = job_purge.purge_job_result_storage(_state(owner_upn="api"))
    assert out["purged"] is False
    assert out["reason"] == "external_job"


def test_purge_missing_storage_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    out = job_purge.purge_job_result_storage(_state(storage_account=None))
    assert out["purged"] is False
    assert out["reason"] == "missing_scope"


def test_purge_deletes_result_and_query_dirs(monkeypatch: pytest.MonkeyPatch, _patch_fs) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.setattr(job_purge, "_is_external", lambda _s: False)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    fs = _patch_fs(_FakeFs())
    out = job_purge.purge_job_result_storage(_state())
    assert out["purged"] is True
    # results/{job_id}, queries/{job_id}, queries/uploads/{job_id}
    assert fs.deleted == ["job-1", "job-1", "uploads/job-1"]
    assert "results/job-1" in out["deleted"]


def test_purge_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.setattr(job_purge, "_is_external", lambda _s: False)

    def _boom() -> Any:
        raise RuntimeError("credential blew up")

    monkeypatch.setattr("api.services.get_credential", _boom)
    out = job_purge.purge_job_result_storage(_state())
    assert out["purged"] is False
    assert out["reason"] == "error"
