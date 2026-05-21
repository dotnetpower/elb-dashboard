"""Integration tests for /api/blast/databases/versions.

Responsibility: Cover the DB Versions tab projection of list_databases().
Edit boundaries: Keep assertions focused on response shape (DbVersionMeta
contract) and degraded fallbacks; do not require live Azure calls.
Key entry points: `client`, `fake_list_databases`,
`test_missing_params_returns_empty`, `test_happy_path_projects_metadata`,
`test_storage_failure_returns_degraded`.
Risky contracts: Response shape must stay aligned with
`web/src/api/blastTools.ts::DbVersionMeta` (db_name, source, source_version,
created_at, _last_modified, optional db_type/title/version_tag) so the SPA
table renders without falling back to "—".
Validation: `uv run pytest -q api/tests/test_blast_databases_versions.py`.
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


_FAKE_DBS: list[dict[str, Any]] = [
    {
        "name": "core_nt",
        "source": "ncbi",
        "source_version": "2026-05-01-01-05-01",
        "downloaded_at": "2026-05-02T03:04:05+00:00",
        "last_modified": "2026-05-02T03:10:00+00:00",
        "title": "Core nucleotide database",
        "molecule_type": "nucl",
        "update_date": "2026-05-01",
        "total_bytes": 304_473_440_256,
    },
    {
        "name": "16S_ribosomal_RNA",
        "source": "ncbi",
        "source_version": "2026-04-30-01-05-01",
        "downloaded_at": "2026-05-01T00:00:00+00:00",
        "last_modified": "2026-05-01T00:00:00+00:00",
        # No title / molecule_type — exercises optional-field omission.
        "total_bytes": 20 * 1024 * 1024,
    },
    {
        "name": "team_amplicon_v3",
        "source": "custom",
        # No source_version — custom DBs may not carry one.
        "downloaded_at": "2026-04-15T10:11:12+00:00",
        "last_modified": "2026-04-15T10:11:12+00:00",
        "molecule_type": "nucl",
        "total_bytes": 200 * 1024 * 1024,
    },
]


@pytest.fixture()
def fake_list_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_cred: Any, _account: str, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        import copy

        return copy.deepcopy(_FAKE_DBS)

    monkeypatch.setattr("api.services.storage_data.list_databases", _fake, raising=True)

    def _no_access(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"action": "noop"}

    monkeypatch.setattr(
        "api.services.storage_public_access.ensure_local_storage_access",
        _no_access,
        raising=True,
    )


def test_missing_params_returns_empty(client: TestClient) -> None:
    """No storage account / resource group -> empty payload, no storage call."""
    r = client.get("/api/blast/databases/versions")
    assert r.status_code == 200
    body = r.json()
    assert body == {"versions": [], "total": 0}


def test_happy_path_projects_metadata(
    client: TestClient, fake_list_databases: None
) -> None:
    r = client.get(
        "/api/blast/databases/versions",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    versions = body["versions"]
    assert [v["db_name"] for v in versions] == [
        "16S_ribosomal_RNA",
        "core_nt",
        "team_amplicon_v3",
    ], "versions must be sorted by db_name"

    by_name = {v["db_name"]: v for v in versions}

    core_nt = by_name["core_nt"]
    assert core_nt["source"] == "ncbi"
    assert core_nt["source_version"] == "2026-05-01-01-05-01"
    assert core_nt["created_at"] == "2026-05-02T03:04:05+00:00"
    assert core_nt["_last_modified"] == "2026-05-02T03:10:00+00:00"
    assert core_nt["db_type"] == "nucl"
    assert core_nt["title"] == "Core nucleotide database"
    assert core_nt["version_tag"] == "2026-05-01"

    rrna = by_name["16S_ribosomal_RNA"]
    assert "db_type" not in rrna, "optional fields must be omitted, not nulled"
    assert "title" not in rrna
    assert "version_tag" not in rrna

    custom = by_name["team_amplicon_v3"]
    assert custom["source"] == "custom"
    assert custom["source_version"] is None
    assert custom["db_type"] == "nucl"


def test_storage_failure_returns_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage outage -> degraded marker, never a 500."""

    class _AuthFail(Exception):
        pass

    def _raise(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise _AuthFail("This request is not authorized to perform this operation.")

    monkeypatch.setattr("api.services.storage_data.list_databases", _raise, raising=True)

    def _no_access(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"action": "noop"}

    monkeypatch.setattr(
        "api.services.storage_public_access.ensure_local_storage_access",
        _no_access,
        raising=True,
    )

    r = client.get(
        "/api/blast/databases/versions",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "storage_account": "stfake",
            "resource_group": "rg-fake",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["versions"] == []
    assert body["total"] == 0
    assert body.get("degraded") is True
    assert "degraded_reason" in body
