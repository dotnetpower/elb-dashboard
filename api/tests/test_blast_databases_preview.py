"""Integration tests for /api/blast/databases/{db}/preview.

Responsibility: Cover the route that surfaces NCBI snapshot facts BEFORE
    the user clicks Download — file count, snapshot id, estimated bytes,
    last-modified, and the "not in current snapshot" fallback.
Edit boundaries: Mock the ncbi_catalogue service; never reach NCBI from CI.
Key entry points: `test_preview_route_returns_available`,
    `test_preview_route_returns_unavailable_db`,
    `test_preview_route_rejects_bad_name`,
    `test_preview_route_502_on_ncbi_denied`,
    `test_preview_route_502_on_ncbi_unavailable`.
Risky contracts: Response keys must stay aligned with web/src/api/blast.ts
    ``blastApi.previewDatabase``.
Validation: `uv run pytest -q api/tests/test_blast_databases_preview.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_preview_route_returns_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "db_name": "16S_ribosomal_RNA",
        "snapshot": "2026-05-21-01-05-02",
        "available": True,
        "file_count": 12,
        "volume_count": 3,
        "total_bytes_estimate": 18_000_000,
        "last_modified": "Thu, 21 May 2026 03:00:00 GMT",
        "signature_key": "2026-05-21-01-05-02/16S_ribosomal_RNA.tar.gz.md5",
        "signature_etag": "md5-etag",
        "files_sample": [],
        "source": "ncbi-s3",
    }
    monkeypatch.setattr(
        "api.services.ncbi_catalogue.preview_database",
        lambda _name: dict(payload),
        raising=True,
    )

    resp = client.get("/api/blast/databases/16S_ribosomal_RNA/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["snapshot"] == payload["snapshot"]
    assert body["file_count"] == 12
    assert body["signature_etag"] == "md5-etag"


def test_preview_route_returns_unavailable_db(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.ncbi_catalogue.preview_database",
        lambda _name: {
            "db_name": "ftp_only_db",
            "snapshot": "2026-05-21-01-05-02",
            "available": False,
            "file_count": 0,
            "volume_count": 0,
            "total_bytes_estimate": 0,
            "last_modified": None,
            "signature_key": None,
            "signature_etag": None,
            "files_sample": [],
            "source": "ncbi-s3",
            "message": "This database is not present in the current NCBI S3 snapshot.",
        },
        raising=True,
    )

    resp = client.get("/api/blast/databases/ftp_only_db/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "snapshot" in body
    assert body["message"]


def test_preview_route_rejects_bad_name(client: TestClient) -> None:
    # The URL still has a valid char set since FastAPI decodes the path; the
    # route's own RE_DB_NAME check enforces the constraint.
    resp = client.get("/api/blast/databases/!!bad!!/preview")
    assert resp.status_code == 400


def test_preview_route_502_on_ncbi_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes.storage.common import NcbiAccessDenied

    def _raise(_name: str) -> Any:
        raise NcbiAccessDenied("NCBI 403")

    monkeypatch.setattr(
        "api.services.ncbi_catalogue.preview_database", _raise, raising=True
    )
    resp = client.get("/api/blast/databases/core_nt/preview")
    assert resp.status_code == 502
    assert "rate-limited" in resp.json()["detail"].lower()


def test_preview_route_502_on_ncbi_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.routes.storage.common import NcbiUnavailable

    def _raise(_name: str) -> Any:
        raise NcbiUnavailable("DNS failure")

    monkeypatch.setattr(
        "api.services.ncbi_catalogue.preview_database", _raise, raising=True
    )
    resp = client.get("/api/blast/databases/core_nt/preview")
    assert resp.status_code == 502
