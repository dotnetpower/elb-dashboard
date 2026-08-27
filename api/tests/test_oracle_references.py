"""Tests for immutable order-oracle reverse references.

Responsibility: Verify deterministic safe paths, create-only writes, and
    idempotent existing-reference handling.
Edit boundaries: In-memory blob fakes only; selection and retention are tested
    in their owning modules.
Key entry points: `test_create_reference_is_create_only_and_idempotent`.
Risky contracts: Job IDs never enter blob paths directly and existing records
    must not be overwritten.
Validation: `uv run pytest -q api/tests/test_oracle_references.py`.
"""

from __future__ import annotations

import json

import pytest
from api.services.db import oracle_references
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError


class _Blob:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.exists = False
        self.deleted = False

    def upload_blob(self, data: str, *, overwrite: bool) -> None:
        self.calls.append((data, overwrite))
        if self.exists:
            raise ResourceExistsError("exists")
        self.exists = True

    def get_blob_properties(self) -> object:
        if not self.exists:
            raise ResourceNotFoundError("missing")
        return object()

    def delete_blob(self) -> None:
        self.deleted = True
        self.exists = False


class _Container:
    def __init__(self, blob: _Blob) -> None:
        self.blob = blob
        self.path = ""
        self.marker = _Blob()

    def get_blob_client(self, path: str) -> _Blob:
        self.path = path
        if "/gc/" in path:
            return self.marker
        return self.blob


def test_create_reference_is_create_only_and_idempotent(monkeypatch) -> None:
    blob = _Blob()
    container = _Container(blob)
    monkeypatch.setattr(
        oracle_references,
        "oracle_container",
        lambda *_args: container,
    )

    first = oracle_references.create_oracle_reference(
        object(),
        storage_account="stelb",
        db_name="core_nt",
        run_id="run-1",
        job_id="job/unsafe-but-hashed",
        source_version="v1",
    )
    second = oracle_references.create_oracle_reference(
        object(),
        storage_account="stelb",
        db_name="core_nt",
        run_id="run-1",
        job_id="job/unsafe-but-hashed",
        source_version="v1",
    )

    assert first == second
    assert "job/unsafe" not in first
    assert first.startswith("metadata/oracles/core_nt/references/run-1/")
    payload = json.loads(blob.calls[0][0])
    assert payload["job_id"] == "job/unsafe-but-hashed"
    assert all(overwrite is False for _data, overwrite in blob.calls)


def test_reference_rejects_retiring_run(monkeypatch) -> None:
    blob = _Blob()
    container = _Container(blob)
    container.marker.exists = True
    monkeypatch.setattr(
        oracle_references,
        "oracle_container",
        lambda *_args: container,
    )

    try:
        oracle_references.create_oracle_reference(
            object(),
            storage_account="stelb",
            db_name="core_nt",
            run_id="run-1",
            job_id="job-1",
            source_version="v1",
        )
    except oracle_references.OracleRunRetiring:
        pass
    else:
        raise AssertionError("retiring run must reject a new reference")

    assert blob.calls == []


def test_reference_rejects_pointer_when_marker_appears_and_cleanup_fails(
    monkeypatch,
) -> None:
    marker = _Blob()

    class _RacingBlob(_Blob):
        def upload_blob(self, data: str, *, overwrite: bool) -> None:
            super().upload_blob(data, overwrite=overwrite)
            marker.exists = True

        def delete_blob(self) -> None:
            raise RuntimeError("transient delete failure")

    reference = _RacingBlob()
    container = _Container(reference)
    container.marker = marker
    monkeypatch.setattr(
        oracle_references,
        "oracle_container",
        lambda *_args: container,
    )

    with pytest.raises(oracle_references.OracleRunRetiring):
        oracle_references.create_oracle_reference(
            object(),
            storage_account="stelb",
            db_name="core_nt",
            run_id="run-1",
            job_id="job-1",
            source_version="v1",
        )

    assert reference.exists is True
