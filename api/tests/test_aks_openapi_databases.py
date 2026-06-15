"""Tests for the cluster-independent BLAST database catalogue control-plane routes.

Responsibility: Lock the projection in
`api.services.openapi.databases` (elb-openapi `/v1/databases` +
`/v1/databases/{db_name}` shapes) and the route contract in
`api.routes.aks.openapi_databases` (Storage-scope resolution, 400 / 404 / 503
status mapping, auth).
Edit boundaries: Test-only. Patch the catalogue enumeration at its source module
(`api.services.storage.database_catalog_cache.list_databases_cached`) for the list
path, the NCBI metadata blob reader
(`api.services.storage.blob_io.read_metadata_blob_bytes` +
`api.services.storage.data._blob_service`) for the detail path, and the failure
classifier (`api.services.storage.data.classify_storage_failure`).
Key entry points: the test functions below.
Risky contracts: Asserts the external list/detail response field set so an
elb-openapi caller can swap host without reshaping its parsing.
Validation: `uv run pytest -q api/tests/test_aks_openapi_databases.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.services.openapi import databases as db_svc
from azure.core.exceptions import ResourceNotFoundError
from fastapi.testclient import TestClient

# --------------------------------------------------------------------------- #
# NCBI metadata blob fakes (mirror the elb-openapi {db}/{db}-*-metadata.json)
# --------------------------------------------------------------------------- #


class _FakeBlobClient:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContainer:
    def get_blob_client(self, name: str) -> _FakeBlobClient:
        return _FakeBlobClient(name)


class _FakeService:
    def get_container_client(self, _container: str) -> _FakeContainer:
        return _FakeContainer()


def _patch_metadata_blobs(
    monkeypatch: pytest.MonkeyPatch, blobs: dict[str, bytes | Exception]
) -> None:
    """Make get_database resolve from an in-memory blob map.

    Keys are the blob path ``{db}/{db}-nucl-metadata.json`` (or ``-prot-``).
    A missing key raises ResourceNotFoundError (the genuine 404 path); an
    Exception value is raised as a transient failure.
    """
    monkeypatch.setattr(
        "api.services.storage.data._blob_service", lambda *a, **k: _FakeService()
    )

    def _fake_read(blob_client: Any, *, label: str = "metadata", **_k: Any) -> bytes:
        value = blobs.get(blob_client.name)
        if value is None:
            raise ResourceNotFoundError(f"missing {blob_client.name}")
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        "api.services.storage.blob_io.read_metadata_blob_bytes", _fake_read
    )


_NUCL_16S = json.dumps(
    {
        "description": "16S ribosomal RNA (Bacteria and Archaea)",
        "dbtype": "Nucleotide",
        "version": "1.2",
        "last-updated": "2026-06-12T14:34:25",
        "number-of-sequences": 27648,
        "number-of-letters": 40000000,
        "number-of-volumes": 1,
        "bytes-total": 1234,
        "bytes-to-cache": 1000,
        "files": ["az://blast-db/2026-06-09-01-05-01/16S_ribosomal_RNA.tar.gz"],
    }
).encode("utf-8")

_PROT_SWISSPROT = json.dumps(
    {
        "title": "UniProtKB/Swiss-Prot",
        "dbtype": "Protein",
        "number-of-sequences": 500,
        "files": ["az://blast-db/2026-05-01-00-00-00/swissprot.tar.gz"],
    }
).encode("utf-8")


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


def test_get_database_projects_nucl_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for the catalogue-cache gap: a single-volume DB like 16S has
    # full molecule_type/title/counts from its NCBI nucl-metadata blob, even
    # though the catalogue's .njs enrichment left them null.
    _patch_metadata_blobs(
        monkeypatch,
        {"16S_ribosomal_RNA/16S_ribosomal_RNA-nucl-metadata.json": _NUCL_16S},
    )
    meta = db_svc.get_database(object(), "stgacct", "16S_ribosomal_RNA")
    assert meta is not None
    assert meta["name"] == "16S_ribosomal_RNA"
    assert meta["container"] == "blast-db"
    assert meta["molecule_type"] == "dna"
    assert meta["molecule_label"] == "mixed DNA"
    assert meta["dbtype"] == "Nucleotide"
    assert meta["title"] == "16S ribosomal RNA (Bacteria and Archaea)"
    assert meta["snapshot"] == "2026-06-09-01-05-01"
    assert meta["last_updated"] == "2026-06-12T14:34:25"
    assert meta["number_of_sequences"] == 27648
    assert meta["number_of_letters"] == 40000000
    assert meta["number_of_volumes"] == 1
    assert meta["bytes_total"] == 1234
    assert meta["bytes_to_cache"] == 1000
    assert meta["metadata_schema_version"] == "1.2"
    assert meta["cached_at"]  # populated ISO timestamp


def test_get_database_falls_through_to_prot(monkeypatch: pytest.MonkeyPatch) -> None:
    # nucl suffix is absent (404) -> the prot suffix is tried and wins.
    _patch_metadata_blobs(
        monkeypatch,
        {"swissprot/swissprot-prot-metadata.json": _PROT_SWISSPROT},
    )
    meta = db_svc.get_database(object(), "stgacct", "swissprot")
    assert meta is not None
    assert meta["molecule_type"] == "protein"
    assert meta["molecule_label"] == "protein"
    assert meta["title"] == "UniProtKB/Swiss-Prot"
    assert meta["snapshot"] == "2026-05-01-00-00-00"


def test_get_database_both_suffixes_absent_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_metadata_blobs(monkeypatch, {})
    assert db_svc.get_database(object(), "stgacct", "core_nt") is None


def test_get_database_transient_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient outage on a candidate must NOT be mistaken for a 404 miss —
    # it is re-raised so the route returns 503.
    boom = RuntimeError("AuthorizationFailure")
    _patch_metadata_blobs(
        monkeypatch,
        {"core_nt/core_nt-nucl-metadata.json": boom},
    )
    with pytest.raises(RuntimeError, match="AuthorizationFailure"):
        db_svc.get_database(object(), "stgacct", "core_nt")


def test_get_database_nucl_404_then_prot_transient_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # nucl 404 (continue) + prot transient -> the transient outweighs the 404.
    _patch_metadata_blobs(
        monkeypatch,
        {"core_nt/core_nt-prot-metadata.json": RuntimeError("timeout")},
    )
    with pytest.raises(RuntimeError, match="timeout"):
        db_svc.get_database(object(), "stgacct", "core_nt")


def test_snapshot_unknown_when_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"dbtype": "Nucleotide"}).encode("utf-8")
    _patch_metadata_blobs(
        monkeypatch, {"x/x-nucl-metadata.json": payload}
    )
    meta = db_svc.get_database(object(), "stgacct", "x")
    assert meta is not None
    assert meta["snapshot"] == "unknown"
    assert meta["number_of_sequences"] is None


def test_resolve_molecule_nucl_and_prot() -> None:
    assert db_svc._resolve_molecule("nucl") == ("dna", "mixed DNA")
    assert db_svc._resolve_molecule("prot") == ("protein", "protein")


def test_raw_int_rejects_bool_and_non_int() -> None:
    assert db_svc._raw_int(True) is None
    assert db_svc._raw_int(0) == 0
    assert db_svc._raw_int(42) == 42
    assert db_svc._raw_int(1.0) == 1
    assert db_svc._raw_int("7") is None
    assert db_svc._raw_int(None) is None


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
    _patch_metadata_blobs(
        monkeypatch,
        {"core_nt/core_nt-nucl-metadata.json": _NUCL_16S},
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
    _patch_metadata_blobs(monkeypatch, {})
    resp = client.get("/api/aks/openapi/databases/core_nt?storage_account=stgq")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_detail_route_not_found_storage_failure_maps_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_metadata_blobs(
        monkeypatch,
        {"core_nt/core_nt-nucl-metadata.json": RuntimeError("AccountNotFound")},
    )
    monkeypatch.setattr(
        "api.services.storage.data.classify_storage_failure",
        lambda *a, **k: {"degraded": True, "degraded_reason": "not_found"},
    )
    resp = client.get("/api/aks/openapi/databases/core_nt?storage_account=stgq")
    assert resp.status_code == 404
    assert resp.json()["degraded_reason"] == "not_found"
