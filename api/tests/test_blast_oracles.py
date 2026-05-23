"""Tests for BLAST Oracles behavior.

Responsibility: Tests for BLAST Oracles behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_strict_tie_order_oracle_expands_candidate_pool`,
`test_non_strict_tie_order_oracle_keeps_candidate_pool`,
`test_upload_tie_order_oracle_writes_finalizer_metadata`,
`test_upload_tie_order_oracle_rejects_oversized_payload`,
`test_upload_tie_order_oracle_rejects_non_string_accession_items`,
`test_upload_db_order_oracle_pointer_writes_url_manifest`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_oracles.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from api.services import blast_oracles
from api.tasks import blast as blast_tasks


def test_strict_tie_order_oracle_expands_candidate_pool() -> None:
    options = blast_tasks._expand_strict_tie_order_candidate_pool(
        {
            "max_target_seqs": 100,
            "tie_order_oracle_accessions": ["OZ254258.1"],
            "tie_order_oracle_strict": True,
        }
    )

    assert options["max_target_seqs"] == 5000
    assert options["requested_max_target_seqs"] == 100


def test_non_strict_tie_order_oracle_keeps_candidate_pool() -> None:
    options = {"max_target_seqs": 100, "tie_order_oracle_accessions": ["OZ254258.1"]}

    assert blast_tasks._expand_strict_tie_order_candidate_pool(options) is options


def test_upload_tie_order_oracle_writes_finalizer_metadata(monkeypatch) -> None:
    uploads: list[dict[str, object]] = []

    def fake_upload_blob_text(*args, **kwargs) -> None:
        uploads.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr("api.services.get_credential", lambda: "credential")
    monkeypatch.setattr("api.services.storage.data.upload_blob_text", fake_upload_blob_text)

    result = blast_oracles.upload_tie_order_oracle_if_present(
        storage_account="stelb",
        job_id="job-123",
        options={
            "tie_order_oracle_accessions": ["PX485240.1", "OX044342.2"],
            "tie_order_oracle_strict": True,
        },
    )

    assert result == {
        "blob_path": "job-123/metadata/tie-order-oracle.txt",
        "accession_count": 2,
        "strict": True,
    }
    assert uploads[:1] == [
        {
            "args": (
                "credential",
                "stelb",
                "results",
                "job-123/metadata/tie-order-oracle.txt",
                "PX485240.1\nOX044342.2\n",
            ),
            "kwargs": {"content_type": "text/plain; charset=utf-8"},
        }
    ]
    assert uploads[1]["args"][3] == "job-123/metadata/tie-order-oracle-strict.txt"


def test_upload_tie_order_oracle_rejects_oversized_payload() -> None:
    oversized = "A" * (blast_oracles.TIE_ORDER_ORACLE_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="too large"):
        blast_oracles.upload_tie_order_oracle_if_present(
            storage_account="stelb",
            job_id="job-123",
            options={"tie_order_oracle_text": oversized},
        )


def test_upload_tie_order_oracle_rejects_non_string_accession_items() -> None:
    with pytest.raises(ValueError, match="must contain only strings"):
        blast_oracles.upload_tie_order_oracle_if_present(
            storage_account="stelb",
            job_id="job-123",
            options={"tie_order_oracle_accessions": ["PX485240.1", None]},
        )


def test_upload_db_order_oracle_pointer_writes_url_manifest(monkeypatch) -> None:
    uploads: list[dict[str, object]] = []
    oracle_calls: list[dict[str, object]] = []

    def fake_upload_blob_text(*args, **kwargs) -> None:
        uploads.append({"args": args, "kwargs": kwargs})

    def fake_part_urls(**kwargs):
        oracle_calls.append(kwargs)
        return [
            "https://stelb.blob.core.windows.net/blast-db/metadata/oracles/core_nt/parts/run/00.txt",
            "https://stelb.blob.core.windows.net/blast-db/metadata/oracles/core_nt/parts/run/01.txt",
        ]

    monkeypatch.setattr("api.services.get_credential", lambda: "credential")
    monkeypatch.setattr("api.services.storage.data.upload_blob_text", fake_upload_blob_text)
    monkeypatch.setattr(
        blast_oracles,
        "resolve_db_metadata",
        lambda *_args: {"source_version": "v1"},
    )
    monkeypatch.setattr(blast_oracles, "db_order_oracle_part_urls", fake_part_urls)

    result = blast_oracles.upload_db_order_oracle_pointer_if_available(
        storage_account="stelb",
        job_id="job-123",
        database="core_nt",
        options={"sharding_mode": "precise", "use_db_order_oracle": True},
    )

    assert result == {
        "blob_path": "job-123/metadata/tie-order-oracle-urls.txt",
        "db_name": "core_nt",
        "part_count": 2,
        "source_version": "v1",
    }
    assert oracle_calls == [
        {
            "storage_account": "stelb",
            "db_name": "core_nt",
            "expected_source_version": "v1",
        }
    ]
    assert uploads[0]["args"][3] == "job-123/metadata/tie-order-oracle-urls.txt"
    assert "00.txt" in uploads[0]["args"][4]
    assert uploads[0]["kwargs"] == {"content_type": "text/plain; charset=utf-8"}


def test_upload_db_order_oracle_pointer_requires_explicit_opt_in() -> None:
    assert (
        blast_oracles.upload_db_order_oracle_pointer_if_available(
            storage_account="stelb",
            job_id="job-123",
            database="core_nt",
            options={"sharding_mode": "precise"},
        )
        is None
    )


def test_db_order_oracle_part_urls_rejects_source_version_mismatch(monkeypatch) -> None:
    class FakeStatusDownload:
        def readall(self) -> bytes:
            return json.dumps(
                {"run_id": "run-1", "expected_parts": 2, "source_version": "old"}
            ).encode("utf-8")

    class FakeBlobClient:
        def download_blob(
            self, *, offset: int = 0, length: int | None = None
        ) -> FakeStatusDownload:
            del offset, length
            return FakeStatusDownload()

    class FakeContainer:
        def get_blob_client(self, _blob_name: str) -> FakeBlobClient:
            return FakeBlobClient()

        def list_blobs(self, *, name_starts_with: str):
            return [
                SimpleNamespace(name=f"{name_starts_with}00.txt"),
                SimpleNamespace(name=f"{name_starts_with}01.txt"),
            ]

    class FakeService:
        def get_container_client(self, _container: str) -> FakeContainer:
            return FakeContainer()

    monkeypatch.setattr("api.services.get_credential", lambda: "credential")
    monkeypatch.setattr("api.services.storage.data._blob_service", lambda *_args: FakeService())

    assert (
        blast_oracles.db_order_oracle_part_urls(
            storage_account="stelb",
            db_name="core_nt",
            expected_source_version="new",
        )
        == []
    )
    assert blast_oracles.db_order_oracle_part_urls(
        storage_account="stelb",
        db_name="core_nt",
        expected_source_version="old",
    ) == [
        "https://stelb.blob.core.windows.net/blast-db/metadata/oracles/core_nt/parts/run-1/00.txt",
        "https://stelb.blob.core.windows.net/blast-db/metadata/oracles/core_nt/parts/run-1/01.txt",
    ]
