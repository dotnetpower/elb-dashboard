"""Tests for the prepare-db readiness gate (`validate_blast_database_ready`).

Responsibility: Make sure the SPA's "I just clicked Submit on a DB that's still
    being downloaded" path is rejected with `database_not_ready` (or
    `database_updating`) before any Celery task is enqueued. Covers both the
    direct service call and the preflight HTTP integration.
Edit boundaries: Patch `_blob_service` in-process and the metadata blob bytes;
    never reach Azure.
Key entry points: `_FakeContainer`, `_install_fake_service`,
    `test_validate_blast_database_ready_*`, `test_preflight_blocks_*_database`.
Risky contracts: `copy_status.phase == "completed"` is the only ready phase.
    Tests reset the readiness cache between cases.
Validation: `uv run pytest -q api/tests/test_blast_database_readiness.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.services.blast.task_config import (
    BlastDatabaseAvailabilityError,
    reset_blast_database_readiness_cache,
    validate_blast_database_ready,
)
from fastapi.testclient import TestClient


class _Blob:
    def __init__(self, name: str) -> None:
        self.name = name


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readall(self) -> bytes:
        return self._payload


class _BlobClient:
    def __init__(self, payload: bytes | None) -> None:
        self._payload = payload

    def download_blob(self, offset: int = 0, length: int | None = None) -> _Reader:
        if self._payload is None:
            raise FileNotFoundError("blob not found")
        data = self._payload
        if length is not None:
            data = data[: length]
        return _Reader(data)


class _Container:
    def __init__(self, names: list[str], metadata: bytes | None) -> None:
        self._names = names
        self._metadata = metadata

    def list_blobs(self, name_starts_with: str = "") -> list[_Blob]:
        return [_Blob(name) for name in self._names if name.startswith(name_starts_with)]

    def get_blob_client(self, blob_name: str) -> _BlobClient:
        # Only the metadata.json client returns content; anything else 404s.
        if blob_name.endswith("-metadata.json"):
            return _BlobClient(self._metadata)
        return _BlobClient(None)


class _Service:
    def __init__(self, names: list[str], metadata: bytes | None) -> None:
        self._names = names
        self._metadata = metadata

    def get_container_client(self, container: str) -> _Container:
        assert container == "blast-db"
        return _Container(self._names, self._metadata)


def _install_fake_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    names: list[str],
    metadata: dict[str, Any] | None,
) -> None:
    payload = json.dumps(metadata).encode("utf-8") if metadata is not None else None
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda _credential, _account: _Service(names, payload),
    )
    reset_blast_database_readiness_cache()


def test_validate_blast_database_ready_passes_when_phase_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_service(
        monkeypatch,
        names=[
            "core_nt/core_nt.00.nhr",
            "core_nt/core_nt.00.nin",
            "core_nt/core_nt.00.nsq",
        ],
        metadata={
            "db_name": "core_nt",
            "copy_status": {"phase": "completed", "success": 800, "total_files": 800},
        },
    )

    result = validate_blast_database_ready(
        storage_account="elbstg01", database="core_nt"
    )
    assert result["marker_blob"].endswith(".nsq")


def test_validate_blast_database_ready_rejects_phase_copying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_service(
        monkeypatch,
        names=[
            "core_nt/core_nt.00.nhr",
            "core_nt/core_nt.00.nsq",
        ],
        metadata={
            "db_name": "core_nt",
            "copy_status": {"phase": "copying", "success": 30, "total_files": 800},
        },
    )

    with pytest.raises(BlastDatabaseAvailabilityError) as err:
        validate_blast_database_ready(
            storage_account="elbstg01", database="core_nt"
        )

    assert err.value.code == "database_not_ready"
    msg = str(err.value)
    assert "phase=copying" in msg
    assert "30/800" in msg


def test_validate_blast_database_ready_rejects_phase_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_service(
        monkeypatch,
        names=["core_nt/core_nt.00.nsq"],
        metadata={
            "copy_status": {"phase": "partial", "success": 750, "total_files": 800},
        },
    )

    with pytest.raises(BlastDatabaseAvailabilityError) as err:
        validate_blast_database_ready(
            storage_account="elbstg01", database="core_nt"
        )

    assert err.value.code == "database_not_ready"


def test_validate_blast_database_ready_rejects_update_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_service(
        monkeypatch,
        names=[
            "core_nt/core_nt.00.nsq",
        ],
        metadata={
            "update_in_progress": True,
            "updating_to_source_version": "BLAST_DB-2026-05-20",
        },
    )

    with pytest.raises(BlastDatabaseAvailabilityError) as err:
        validate_blast_database_ready(
            storage_account="elbstg01", database="core_nt"
        )

    assert err.value.code == "database_updating"
    assert "BLAST_DB-2026-05-20" in str(err.value)


def test_validate_blast_database_ready_passes_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy DBs prepared before the hardening have no metadata.json — falling
    back to availability semantics keeps them usable."""
    _install_fake_service(
        monkeypatch,
        names=[
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nhr",
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nsq",
        ],
        metadata=None,
    )

    result = validate_blast_database_ready(
        storage_account="elbstg01", database="16S_ribosomal_RNA"
    )
    assert result["marker_blob"].endswith(".nsq")


def test_preflight_blocks_in_flight_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: preflight rejects with status=fail when copy_status=copying."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "aks-elb", "power_state": "Running"}],
    )
    _install_fake_service(
        monkeypatch,
        names=[
            "core_nt/core_nt.00.nhr",
            "core_nt/core_nt.00.nsq",
        ],
        metadata={
            "db_name": "core_nt",
            "copy_status": {"phase": "copying", "success": 30, "total_files": 800},
        },
    )

    from api.main import app

    response = TestClient(app).post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "storage_account": "elbstg01",
            "db": "core_nt",
            "query_data": ">q1\nAAAA\n",
            "sharding_mode": "off",
            "outfmt": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    db_check = next(item for item in body["checks"] if item["id"] == "database")
    assert db_check["status"] == "fail"
    assert db_check["severity"] == "critical"
    assert db_check.get("error_code") == "database_not_ready"
    assert "30/800" in db_check["detail"]
    assert body["ready"] is False
