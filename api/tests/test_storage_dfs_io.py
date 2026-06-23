"""Tests for the dfs (ADLS Gen2) result listing + the blob_io dispatch.

Responsibility: Prove ``dfs_io.list_paths_dfs`` returns the same row shape as the
Blob listing (file_id / name / size / last_modified), filters out directory
entries, normalizes last_modified, degrades a missing directory to ``[]``, and
that ``blob_io.list_result_blobs`` dispatches to dfs only when
``STORAGE_DFS_ENABLED`` is on and falls back to Blob on a dfs error.
Edit boundaries: Listing/dispatch behaviour only. No real Azure network — the
filesystem + path objects are fakes.
Key entry points: ``test_list_paths_dfs_*``, ``test_dispatch_*``.
Risky contracts: directory entries MUST be filtered (Blob yields only blobs);
a ResourceNotFoundError directory MUST degrade to ``[]``.
Validation: ``uv run pytest -q api/tests/test_storage_dfs_io.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from api.services.storage import blob_io, dfs_io
from api.services.storage.blob_ids import encode_blob_file_id
from azure.core.exceptions import ResourceNotFoundError


class _Path:
    def __init__(
        self, name: str, *, is_directory: bool = False, content_length: int = 0, last_modified=None
    ) -> None:
        self.name = name
        self.is_directory = is_directory
        self.content_length = content_length
        self.last_modified = last_modified


class _FakeFs:
    def __init__(self, paths: list[_Path], *, raise_not_found: bool = False) -> None:
        self._paths = paths
        self._raise_not_found = raise_not_found
        self.requested_path: Any = "UNSET"

    def get_paths(self, *, path: Any, recursive: bool) -> list[_Path]:
        self.requested_path = path
        assert recursive is True
        if self._raise_not_found:
            raise ResourceNotFoundError("filesystem path not found")
        return list(self._paths)


@pytest.fixture
def _patch_fs(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, _FakeFs] = {}

    def _install(fs: _FakeFs) -> _FakeFs:
        holder["fs"] = fs
        monkeypatch.setattr(
            "api.services.storage.dfs_client_pool._dfs_filesystem",
            lambda *_a, **_k: fs,
        )
        return fs

    return _install


def test_list_paths_dfs_row_shape_and_dir_filter(_patch_fs) -> None:
    dt = datetime(2026, 6, 23, 9, 0, 0, tzinfo=UTC)
    _patch_fs(
        _FakeFs(
            [
                _Path("job-1", is_directory=True),  # filtered out
                _Path("job-1/metadata", is_directory=True),  # filtered out
                _Path("job-1/metadata/SUCCESS.txt", content_length=12, last_modified=dt),
                _Path("job-1/result.out.gz", content_length=2048, last_modified=dt),
            ]
        )
    )
    rows = dfs_io.list_paths_dfs(object(), "elbstg01", "results", "job-1/", limit=5000)
    assert [r["name"] for r in rows] == [
        "job-1/metadata/SUCCESS.txt",
        "job-1/result.out.gz",
    ]
    assert rows[0]["file_id"] == encode_blob_file_id("job-1/metadata/SUCCESS.txt")
    assert rows[0]["size"] == 12
    assert rows[0]["last_modified"] == dt.isoformat()
    assert rows[1]["size"] == 2048


def test_list_paths_dfs_normalizes_string_last_modified(_patch_fs) -> None:
    _patch_fs(
        _FakeFs(
            [
                _Path(
                    "job-2/r.out",
                    content_length=1,
                    last_modified="Tue, 23 Jun 2026 09:00:00 GMT",
                )
            ]
        )
    )
    rows = dfs_io.list_paths_dfs(object(), "elbstg01", "results", "job-2/", limit=10)
    assert rows[0]["last_modified"] == "Tue, 23 Jun 2026 09:00:00 GMT"


def test_list_paths_dfs_missing_dir_returns_empty(_patch_fs) -> None:
    _patch_fs(_FakeFs([], raise_not_found=True))
    assert dfs_io.list_paths_dfs(object(), "elbstg01", "results", "job-x/", limit=10) == []


def test_list_paths_dfs_strips_trailing_slash_for_dir(_patch_fs) -> None:
    fs = _patch_fs(_FakeFs([_Path("job-3/r.out", content_length=1)]))
    dfs_io.list_paths_dfs(object(), "elbstg01", "results", "2026/06/23/job-3/", limit=10)
    assert fs.requested_path == "2026/06/23/job-3"


def test_list_paths_dfs_honours_limit(_patch_fs) -> None:
    _patch_fs(_FakeFs([_Path(f"job-4/r{i}.out", content_length=1) for i in range(10)]))
    rows = dfs_io.list_paths_dfs(object(), "elbstg01", "results", "job-4/", limit=3)
    assert len(rows) == 3


# --- blob_io.list_result_blobs dispatch ------------------------------------


class _FakeBlob:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size
        self.last_modified = None


class _FakeContainerClient:
    def __init__(self, blobs: list[_FakeBlob]) -> None:
        self._blobs = blobs

    def list_blobs(self, *, name_starts_with: str) -> list[_FakeBlob]:
        return [b for b in self._blobs if b.name.startswith(name_starts_with)]


class _FakeSvc:
    def __init__(self, blobs: list[_FakeBlob]) -> None:
        self._cc = _FakeContainerClient(blobs)

    def get_container_client(self, _container: str) -> _FakeContainerClient:
        return self._cc


def test_dispatch_blob_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DFS_ENABLED", raising=False)
    monkeypatch.setattr(
        blob_io, "_blob_service", lambda *_a, **_k: _FakeSvc([_FakeBlob("job-9/r.out", 5)])
    )
    # dfs must NOT be consulted with the flag off.
    monkeypatch.setattr(
        "api.services.storage.dfs_io.list_paths_dfs",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dfs used while flag off")),
    )
    rows = blob_io.list_result_blobs(object(), "elbstg01", "results", "job-9/")
    assert [r["name"] for r in rows] == ["job-9/r.out"]


def test_dispatch_dfs_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.setattr(
        "api.services.storage.dfs_io.list_paths_dfs",
        lambda *_a, **_k: [
            {"file_id": "x", "name": "job-9/r.out", "size": 5, "last_modified": None}
        ],
    )
    rows = blob_io.list_result_blobs(object(), "elbstg01", "results", "job-9/")
    assert [r["name"] for r in rows] == ["job-9/r.out"]


def test_dispatch_falls_back_to_blob_on_dfs_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DFS_ENABLED", "true")
    monkeypatch.setattr(
        "api.services.storage.dfs_io.list_paths_dfs",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dfs down")),
    )
    monkeypatch.setattr(
        blob_io, "_blob_service", lambda *_a, **_k: _FakeSvc([_FakeBlob("job-9/fallback.out", 7)])
    )
    rows = blob_io.list_result_blobs(object(), "elbstg01", "results", "job-9/")
    assert [r["name"] for r in rows] == ["job-9/fallback.out"]
