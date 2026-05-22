"""Unit tests for the NCBI catalogue preview + per-DB update signature helpers.

Responsibility: Cover the dry-run NCBI snapshot summary surface used by the
    "show version before download" UX and the per-DB update detection that
    replaces the latest-dir global comparison.
Edit boundaries: Mock httpx + the snapshot/list helpers in common; never touch
    the real NCBI bucket from CI.
Key entry points: `test_preview_returns_snapshot_facts`,
    `test_preview_marks_db_unavailable_when_snapshot_misses_db`,
    `test_preview_uses_md5_signature_when_present`,
    `test_preview_rejects_invalid_name`,
    `test_database_update_signature_mirrors_preview`.
Risky contracts: Public ``preview_database`` keys are part of the SPA
    contract; keep them stable.
Validation: `uv run pytest -q api/tests/test_ncbi_catalogue.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.routes.storage import common as storage_common
from api.services import ncbi_catalogue


class _FakeHead:
    def __init__(self, etag: str, last_modified: str, size: int) -> None:
        self.status_code = 200
        self.headers = {
            "ETag": f'"{etag}"',
            "Last-Modified": last_modified,
            "Content-Length": str(size),
        }


class _FakeHttpxClient:
    """Minimal HEAD/get stand-in matching httpx.Client API."""

    def __init__(self, head_map: dict[str, _FakeHead]) -> None:
        self._head_map = head_map

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def head(self, url: str, timeout: float | None = None) -> _FakeHead:
        for key, response in self._head_map.items():
            if url.endswith(key):
                return response
        return _FakeHead("missing", "Thu, 01 Jan 1970 00:00:00 GMT", 0)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    storage_common.reset_ncbi_catalogue_cache()
    ncbi_catalogue._reset_preview_cache()


def _stub_snapshot(monkeypatch: pytest.MonkeyPatch, snapshot: str) -> None:
    monkeypatch.setattr(
        ncbi_catalogue,
        "_resolve_latest_dir",
        lambda: snapshot,
        raising=True,
    )


def _stub_keys(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: str,
    db: str,
    keys: list[str],
) -> None:
    def _fake(latest: str, requested: str) -> list[str]:
        assert latest == snapshot
        assert requested == db
        return list(keys)

    monkeypatch.setattr(ncbi_catalogue, "_list_keys", _fake, raising=True)


def _stub_httpx_client(
    monkeypatch: pytest.MonkeyPatch, head_map: dict[str, _FakeHead]
) -> None:
    monkeypatch.setattr(
        ncbi_catalogue,
        "httpx",
        type(
            "_httpx",
            (),
            {
                "Client": lambda *args, **kwargs: _FakeHttpxClient(head_map),
                "HTTPError": ncbi_catalogue.httpx.HTTPError,
            },
        ),
        raising=True,
    )


def test_preview_returns_snapshot_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = "2026-05-21-01-05-02"
    db = "16S_ribosomal_RNA"
    keys = [
        f"{snapshot}/16S_ribosomal_RNA.tar.gz",
        f"{snapshot}/16S_ribosomal_RNA.tar.gz.md5",
        f"{snapshot}/16S_ribosomal_RNA.nhr",
        f"{snapshot}/16S_ribosomal_RNA.nin",
        f"{snapshot}/16S_ribosomal_RNA.nsq",
    ]
    _stub_snapshot(monkeypatch, snapshot)
    _stub_keys(monkeypatch, snapshot=snapshot, db=db, keys=keys)
    _stub_httpx_client(
        monkeypatch,
        {
            "/16S_ribosomal_RNA.tar.gz.md5": _FakeHead(
                "md5-etag", "Thu, 21 May 2026 03:00:00 GMT", 128
            ),
            "/16S_ribosomal_RNA.nhr": _FakeHead(
                "nhr-etag", "Thu, 21 May 2026 03:00:00 GMT", 5_000_000
            ),
        },
    )

    summary = ncbi_catalogue.preview_database(db)
    assert summary["available"] is True
    assert summary["snapshot"] == snapshot
    assert summary["file_count"] == len(keys)
    assert summary["volume_count"] == 3  # nhr, nin, nsq
    assert summary["signature_key"].endswith(".tar.gz.md5")
    assert summary["signature_etag"] == "md5-etag"
    assert summary["last_modified"]
    assert summary["total_bytes_estimate"] > 0


def test_preview_marks_db_unavailable_when_snapshot_misses_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = "2026-05-21-01-05-02"
    db = "ftp_only_db"
    _stub_snapshot(monkeypatch, snapshot)
    _stub_keys(monkeypatch, snapshot=snapshot, db=db, keys=[])

    summary = ncbi_catalogue.preview_database(db)
    assert summary["available"] is False
    assert summary["snapshot"] == snapshot
    assert summary["file_count"] == 0
    assert "FTP" in (summary.get("message") or "") or "snapshot" in (
        summary.get("message") or ""
    )


def test_preview_uses_md5_signature_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = "2026-05-21-01-05-02"
    db = "core_nt"
    keys = [
        f"{snapshot}/core_nt.000.tar.gz",
        f"{snapshot}/core_nt.000.tar.gz.md5",
        f"{snapshot}/core_nt.001.tar.gz",
        f"{snapshot}/core_nt.001.tar.gz.md5",
        f"{snapshot}/core_nt.000.nhr",
    ]
    _stub_snapshot(monkeypatch, snapshot)
    _stub_keys(monkeypatch, snapshot=snapshot, db=db, keys=keys)
    _stub_httpx_client(
        monkeypatch,
        {
            "/core_nt.000.tar.gz.md5": _FakeHead(
                "md5-000", "Thu, 21 May 2026 03:00:00 GMT", 64
            ),
            "/core_nt.000.nhr": _FakeHead(
                "nhr-000", "Thu, 21 May 2026 03:00:00 GMT", 9_000_000
            ),
        },
    )

    summary = ncbi_catalogue.preview_database(db)
    assert summary["signature_key"] == f"{snapshot}/core_nt.000.tar.gz.md5"
    assert summary["signature_etag"] == "md5-000"


def test_preview_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        ncbi_catalogue.preview_database("../etc/passwd")


def test_database_update_signature_mirrors_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = "2026-05-21-01-05-02"
    db = "swissprot"
    keys = [
        f"{snapshot}/swissprot.tar.gz",
        f"{snapshot}/swissprot.tar.gz.md5",
        f"{snapshot}/swissprot.phr",
    ]
    _stub_snapshot(monkeypatch, snapshot)
    _stub_keys(monkeypatch, snapshot=snapshot, db=db, keys=keys)
    _stub_httpx_client(
        monkeypatch,
        {
            "/swissprot.tar.gz.md5": _FakeHead(
                "sig-etag", "Thu, 21 May 2026 03:00:00 GMT", 32
            ),
            "/swissprot.phr": _FakeHead(
                "phr-etag", "Thu, 21 May 2026 03:00:00 GMT", 300_000_000
            ),
        },
    )

    signature = ncbi_catalogue.database_update_signature(db)
    assert signature["snapshot"] == snapshot
    assert signature["available"] is True
    assert signature["signature_etag"] == "sig-etag"
    assert signature["signature_key"].endswith(".tar.gz.md5")
