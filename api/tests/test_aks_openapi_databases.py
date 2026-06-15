"""Tests for the cluster-independent BLAST database catalogue control-plane routes.

Responsibility: Lock the projection in
`api.services.openapi.databases` (elb-openapi `/v1/databases` +
`/v1/databases/{db_name}` shapes) and the route contract in
`api.routes.aks.openapi_databases` (Storage-scope resolution, 400 / 404 / 503
status mapping, auth).
Edit boundaries: Test-only. Patch the catalogue enumeration at its source module
(`api.services.storage.database_catalog_cache.list_databases_cached`) and the
failure classifier (`api.services.storage.data.classify_storage_failure`).
Key entry points: the test functions below.
Risky contracts: Asserts the external list/detail response field set so an
elb-openapi caller can swap host without reshaping its parsing.
Validation: `uv run pytest -q api/tests/test_aks_openapi_databases.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.openapi import databases as db_svc
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# Service-level projection tests
# --------------------------------------------------------------------------- #


def test_list_databases_dedups_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {"name": "nr"},
        {"name": "core_nt"},
        {"name": "nr"},
        {"name": ""},
        {"name": "  16S_ribosomal_RNA  "},
        "not-a-dict",
    ]
    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        lambda *a, **k: entries,
    )
    out = db_svc.list_databases(object(), "stgacct")
    assert out["container"] == "blast-db"
    assert out["count"] == 3
    assert out["databases"] == [
        {"name": "16S_ribosomal_RNA"},
        {"name": "core_nt"},
        {"name": "nr"},
    ]


def test_get_database_projects_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {
            "name": "core_nt",
            "molecule_type": "Nucleotide",
            "title": "Core nucleotide",
            "source_version": "2026-06-01",
            "update_date": "2026/06/01",
            "total_sequences": 123,
            "total_letters": 456,
            "bytes_total": 789,
            "bytes_to_cache": 321,
        }
    ]
    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        lambda *a, **k: entries,
    )
    meta = db_svc.get_database(object(), "stgacct", "core_nt")
    assert meta is not None
    assert meta["name"] == "core_nt"
    assert meta["container"] == "blast-db"
    assert meta["molecule_type"] == "dna"
    assert meta["molecule_label"] == "mixed DNA"
    assert meta["dbtype"] == "Nucleotide"
    assert meta["snapshot"] == "2026-06-01"
    assert meta["last_updated"] == "2026/06/01"
    assert meta["number_of_sequences"] == 123
    assert meta["number_of_letters"] == 456
    assert meta["bytes_total"] == 789
    assert meta["bytes_to_cache"] == 321
    assert meta["cached_at"]  # populated ISO timestamp


def test_get_database_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        lambda *a, **k: [{"name": "nr"}],
    )
    assert db_svc.get_database(object(), "stgacct", "core_nt") is None


def test_resolve_molecule_protein_and_unknown() -> None:
    assert db_svc._resolve_molecule("Protein") == ("protein", "protein")
    assert db_svc._resolve_molecule("weird") == ("weird", "weird")
    assert db_svc._resolve_molecule("") == (None, "")
    assert db_svc._resolve_molecule(None) == (None, "")


# --------------------------------------------------------------------------- #
# Route tests
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    # Keep the local-debug storage auto-open a no-op in tests.
    monkeypatch.setattr(
        "api.routes.aks.openapi_databases._maybe_open_local_storage_access",
        lambda *a, **k: {"action": "noop"},
    )
    from api.main import app

    return TestClient(app)


def test_list_route_uses_env_storage_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stgenv")
    captured: dict[str, Any] = {}

    def _fake_list(cred: Any, account: str, container: str = "blast-db") -> list[dict[str, Any]]:
        captured["account"] = account
        return [{"name": "nr"}, {"name": "core_nt"}]

    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        _fake_list,
    )
    resp = client.get("/api/aks/openapi/databases")
    assert resp.status_code == 200
    body = resp.json()
    assert captured["account"] == "stgenv"
    assert body["count"] == 2
    assert body["databases"] == [{"name": "core_nt"}, {"name": "nr"}]


def test_list_route_missing_account_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    resp = client.get("/api/aks/openapi/databases")
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_parameters"


def test_list_route_storage_failure_degrades_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: Any, **k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("AuthorizationFailure")

    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        _boom,
    )
    monkeypatch.setattr(
        "api.services.storage.data.classify_storage_failure",
        lambda *a, **k: {"degraded": True, "degraded_reason": "network_blocked"},
    )
    resp = client.get("/api/aks/openapi/databases?storage_account=stgq")
    assert resp.status_code == 503
    body = resp.json()
    assert body["degraded"] is True
    assert body["databases"] == []


def test_detail_route_returns_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        lambda *a, **k: [{"name": "core_nt", "molecule_type": "Nucleotide"}],
    )
    resp = client.get("/api/aks/openapi/databases/core_nt?storage_account=stgq")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "core_nt"
    assert body["molecule_type"] == "dna"
    assert body["container"] == "blast-db"


def test_detail_route_invalid_name_returns_400(client: TestClient) -> None:
    resp = client.get("/api/aks/openapi/databases/bad%20name?storage_account=stgq")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_db_name"


def test_detail_route_missing_account_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STORAGE_ACCOUNT_NAME", raising=False)
    resp = client.get("/api/aks/openapi/databases/core_nt")
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_parameters"


def test_detail_route_unknown_db_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        lambda *a, **k: [{"name": "nr"}],
    )
    resp = client.get("/api/aks/openapi/databases/core_nt?storage_account=stgq")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_detail_route_not_found_storage_failure_maps_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: Any, **k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("AccountNotFound")

    monkeypatch.setattr(
        "api.services.storage.database_catalog_cache.list_databases_cached",
        _boom,
    )
    monkeypatch.setattr(
        "api.services.storage.data.classify_storage_failure",
        lambda *a, **k: {"degraded": True, "degraded_reason": "not_found"},
    )
    resp = client.get("/api/aks/openapi/databases/core_nt?storage_account=stgq")
    assert resp.status_code == 404
    assert resp.json()["degraded_reason"] == "not_found"
