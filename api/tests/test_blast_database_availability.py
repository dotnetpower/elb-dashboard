"""Tests for BLAST database Storage availability admission checks.

Responsibility: Tests for BLAST database Storage availability admission checks.
Edit boundaries: Keep assertions focused on selected-DB existence checks and submit fail-fast
behavior.
Key entry points: `test_validate_blast_database_available_accepts_sequence_marker`,
`test_validate_blast_database_available_rejects_missing_prefix`,
`test_submit_fails_before_elastic_blast_when_database_missing`.
Risky contracts: Do not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_blast_database_availability.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast_task_config import (
    BlastDatabaseAvailabilityError,
    validate_blast_database_available,
)
from api.tasks import blast
from fastapi.testclient import TestClient


class _Blob:
    def __init__(self, name: str) -> None:
        self.name = name


class _Container:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_blobs(self, name_starts_with: str = "") -> list[_Blob]:
        return [_Blob(name) for name in self._names if name.startswith(name_starts_with)]


class _Service:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def get_container_client(self, container: str) -> _Container:
        assert container == "blast-db"
        return _Container(self._names)


def _patch_blob_names(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda _credential, account_name: _Service(names),
    )


def test_validate_blast_database_available_accepts_sequence_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_blob_names(
        monkeypatch,
        [
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nhr",
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nin",
            "16S_ribosomal_RNA/16S_ribosomal_RNA.nsq",
        ],
    )

    result = validate_blast_database_available(
        storage_account="elbstg01",
        database="16S_ribosomal_RNA",
    )

    assert result["blob_prefix"] == "16S_ribosomal_RNA/16S_ribosomal_RNA"
    assert result["marker_blob"] == "16S_ribosomal_RNA/16S_ribosomal_RNA.nsq"


def test_validate_blast_database_available_rejects_missing_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_blob_names(monkeypatch, ["core_nt/core_nt.00.nsq"])

    with pytest.raises(BlastDatabaseAvailabilityError) as err:
        validate_blast_database_available(
            storage_account="elbstg01",
            database="16S_ribosomal_RNA",
        )

    assert err.value.code == "database_not_found"
    assert "Download or prepare this database" in str(err.value)


def test_submit_fails_before_elastic_blast_when_database_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[tuple[str, str, dict[str, Any]]] = []

    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr(blast, "_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        blast,
        "_suppress_sharding_for_unsharded_database",
        lambda **kwargs: kwargs.get("options"),
    )
    monkeypatch.setattr(
        blast,
        "_validate_blast_database_available",
        lambda **_kwargs: (_ for _ in ()).throw(
            blast.BlastDatabaseAvailabilityError(
                "BLAST database '16S_ribosomal_RNA' is not available in Storage.",
                code="database_not_found",
            )
        ),
    )

    def fail_stream(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("elastic-blast submit must not run when the DB is missing")

    monkeypatch.setattr(blast, "_stream_submit_command", fail_stream)

    result = blast.submit.run(
        job_id="job-123",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        storage_account="elbstg01",
        program="blastn",
        database="16S_ribosomal_RNA",
        query_file="queries/q.fa",
        options={"sharding_mode": "off", "disable_sharding": True},
    )

    assert result["status"] == "failed"
    assert result["phase"] == "database_unavailable"
    assert result["error_code"] == "database_not_found"
    assert updates[-1][1] == "database_unavailable"
    assert updates[-1][2]["status"] == "failed"
    assert "not available in Storage" in str(updates[-1][2]["output"])


def test_preflight_blocks_missing_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "aks-elb", "power_state": "Running"}],
    )
    monkeypatch.setattr(
        "api.services.blast_task_config.validate_blast_database_available",
        lambda **_kwargs: (_ for _ in ()).throw(
            BlastDatabaseAvailabilityError(
                "BLAST database '16S_ribosomal_RNA' is not available in Storage.",
                code="database_not_found",
            )
        ),
    )
    # The preflight route now goes through `validate_blast_database_ready`
    # (which wraps `validate_blast_database_available` plus a metadata-blob
    # readiness check). Patch the inner availability function at its source
    # module so the wrapped call sees the boom too.
    monkeypatch.setattr(
        "api.services.blast.task_config.validate_blast_database_available",
        lambda **_kwargs: (_ for _ in ()).throw(
            BlastDatabaseAvailabilityError(
                "BLAST database '16S_ribosomal_RNA' is not available in Storage.",
                code="database_not_found",
            )
        ),
    )
    # The readiness verdict is cached per-process for 5s; clear it so this
    # test does not pick up a stale OK from a sibling test that ran first.
    from api.services.blast.task_config import reset_blast_database_readiness_cache

    reset_blast_database_readiness_cache()

    from api.main import app

    response = TestClient(app).post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "storage_account": "elbstg01",
            "db": "16S_ribosomal_RNA",
            "query_data": ">q1\nAAAA\n",
            "sharding_mode": "off",
            "outfmt": 5,
        },
    )

    assert response.status_code == 200
    body = response.json()
    database_check = next(item for item in body["checks"] if item["id"] == "database")
    assert database_check["status"] == "fail"
    assert database_check["severity"] == "critical"
    assert body["ready"] is False
