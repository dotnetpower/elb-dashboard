"""Tests for NCBI Direct task generation verification.

Responsibility: Lock the marker hash, archive completeness, unique extracted
    file identity, and exact staged-blob size gates that precede promotion.
Edit boundaries: Pure fake-Storage tests for `_verify_generation`; Kubernetes
    orchestration is covered by manifest and dispatch tests.
Key entry points: Tests for `api.tasks.storage.prepare_db_direct`.
Risky contracts: A partial or mixed manifest must never reach active promotion.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_task.py`.
"""

import json
from types import SimpleNamespace

import pytest
from api.tasks.storage.prepare_db_direct import _verify_generation


class _Download:
    def __init__(self, text: str) -> None:
        self.text = text

    def download_blob(self, **_kwargs: object) -> "_Download":
        return self

    def readall(self) -> bytes:
        return self.text.encode()

    def get_blob_properties(self) -> SimpleNamespace:
        return SimpleNamespace(size=len(self.text))


class _Container:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def list_blobs(self, name_starts_with: str = "") -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name=name, size=len(value))
            for name, value in self.blobs.items()
            if name.startswith(name_starts_with)
        ]

    def get_blob_client(self, name: str) -> _Download:
        return _Download(self.blobs[name].decode())


def _marker(transfer_sha: str, name: str, size: int) -> bytes:
    return json.dumps(
        {
            "index": 0,
            "transfer_manifest_sha256": transfer_sha,
            "files": [{"name": name, "size": size}],
        }
    ).encode()


def test_verify_generation_requires_marker_and_exact_blob_size() -> None:
    transfer_sha = "a" * 64
    data = b"database"
    prefix = "core_nt/generations/ncbi-direct-20260819-aaaaaaaaaaaa"
    container = _Container(
        {
            f"{prefix}/core_nt.00.nsq": data,
            f"{prefix}/.manifests/00.json": _marker(transfer_sha, "core_nt.00.nsq", len(data)),
        }
    )

    files, size = _verify_generation(
        container,
        data_prefix=prefix,
        archive_count=1,
        transfer_sha=transfer_sha,
    )

    assert files == ["core_nt.00.nsq"]
    assert size == len(data)


def test_verify_generation_rejects_transfer_hash_mismatch() -> None:
    prefix = "core_nt/generations/ncbi-direct-20260819-aaaaaaaaaaaa"
    container = _Container(
        {
            f"{prefix}/core_nt.00.nsq": b"database",
            f"{prefix}/.manifests/00.json": _marker("b" * 64, "core_nt.00.nsq", 8),
        }
    )

    with pytest.raises(RuntimeError, match="transfer hash mismatch"):
        _verify_generation(
            container,
            data_prefix=prefix,
            archive_count=1,
            transfer_sha="a" * 64,
        )


def test_verify_generation_rejects_missing_staged_file() -> None:
    transfer_sha = "a" * 64
    prefix = "core_nt/generations/ncbi-direct-20260819-aaaaaaaaaaaa"
    container = _Container(
        {
            f"{prefix}/.manifests/00.json": _marker(transfer_sha, "core_nt.00.nsq", 8),
        }
    )

    with pytest.raises(RuntimeError, match="files incomplete"):
        _verify_generation(
            container,
            data_prefix=prefix,
            archive_count=1,
            transfer_sha=transfer_sha,
        )
