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
from api.services.storage import direct_promotion
from api.services.storage.direct_promotion import verify_direct_generation
from api.tasks.storage.prepare_db_direct import _ownership_loss_outcome


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

    files, size = verify_direct_generation(
        container,
        data_prefix=prefix,
        archive_count=1,
        transfer_sha=transfer_sha,
    )

    assert files == ["core_nt.00.nsq"]
    assert size == len(data)


def test_direct_ownership_loss_classifies_cancel_without_touching_metadata() -> None:
    assert (
        _ownership_loss_outcome(
            {
                "update_in_progress": False,
                "pending_generation": {"phase": "cancelled"},
            },
            "old-owner",
        )
        == "cancelled"
    )
    assert (
        _ownership_loss_outcome(
            {
                "update_in_progress": True,
                "prepare_operation_id": "new-owner",
                "pending_generation": {"phase": "queued"},
            },
            "old-owner",
        )
        == "superseded"
    )
    assert (
        _ownership_loss_outcome(
            {
                "update_in_progress": True,
                "prepare_operation_id": "old-owner",
                "pending_generation": {"phase": "downloading"},
            },
            "old-owner",
        )
        is None
    )


def test_verify_generation_rejects_transfer_hash_mismatch() -> None:
    prefix = "core_nt/generations/ncbi-direct-20260819-aaaaaaaaaaaa"
    container = _Container(
        {
            f"{prefix}/core_nt.00.nsq": b"database",
            f"{prefix}/.manifests/00.json": _marker("b" * 64, "core_nt.00.nsq", 8),
        }
    )

    with pytest.raises(RuntimeError, match="transfer hash mismatch"):
        verify_direct_generation(
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
        verify_direct_generation(
            container,
            data_prefix=prefix,
            archive_count=1,
            transfer_sha=transfer_sha,
        )


def test_promote_direct_generation_atomically_switches_active_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_sha = "a" * 64
    generation = "ncbi-direct-20260819-aaaaaaaaaaaa"
    prefix = f"core_nt/generations/{generation}"
    data = b"database"
    container = _Container(
        {
            f"{prefix}/core_nt.00.nsq": data,
            f"{prefix}/.manifests/00.json": _marker(transfer_sha, "core_nt.00.nsq", len(data)),
        }
    )
    container.metadata = {
        "db_name": "core_nt",
        "source_version": "2026-07-21-01-05-02",
        "active_generation": {"id": "old-generation"},
        "update_in_progress": True,
        "prepare_operation_id": "owner",
        "pending_generation": {
            "id": generation,
            "data_prefix": prefix,
            "source_provider": "ncbi-direct",
            "transfer_manifest_sha256": transfer_sha,
            "archive_count": 1,
            "phase": "downloading",
            "source_release_at": "2026-08-19T00:00:00",
            "release_fingerprint": "b" * 64,
            "number_of_letters": 100,
            "number_of_sequences": 10,
            "bytes_total": 200,
            "taxonomy_release_at": "2026-08-26T00:00:00",
            "taxonomy_release_fingerprint": "c" * 64,
        },
        "aks_job_ref": {"job_name": "prepare-core"},
        "db_order_oracle": {"status": "ready"},
    }

    monkeypatch.setattr(
        direct_promotion,
        "download_blob_with_etag",
        lambda _container, _db: (dict(container.metadata), "etag"),
    )

    def update(_container: object, _db: str, _account: str, mutator: object) -> dict[str, object]:
        container.metadata = mutator(dict(container.metadata))  # type: ignore[operator]
        return dict(container.metadata)

    monkeypatch.setattr(direct_promotion, "update_metadata", update)
    monkeypatch.setattr(
        "api.services.ncbi_direct_lock.claim_or_refresh_direct_lock",
        lambda _owner: True,
    )
    monkeypatch.setattr(
        "api.services.ncbi_direct_lock.release_direct_lock",
        lambda _owner: True,
    )
    monkeypatch.setattr(
        direct_promotion,
        "ensure_shard_sets",
        lambda *_args, **_kwargs: {
            "layout_schema": 1,
            "total_volumes": 1,
            "shard_sets": [1],
            "errors": [],
        },
    )

    result = direct_promotion.recover_direct_generation(
        credential=None,
        storage_account="acct",
        db_name="core_nt",
        metadata=dict(container.metadata),
        container=container,
    )

    assert result["outcome"] == "promoted"
    assert container.metadata["active_generation"]["id"] == generation
    assert container.metadata["previous_generation"] == {"id": "old-generation"}
    assert container.metadata["source_provider"] == "ncbi-direct"
    assert container.metadata["source_release_at"] == "2026-08-19T00:00:00"
    assert container.metadata["shard_sets"] == [1]
    assert container.metadata["update_in_progress"] is False
    assert "pending_generation" not in container.metadata
    assert "prepare_operation_id" not in container.metadata
    assert container.metadata["db_order_oracle"]["status"] == "stale"

    repeated = direct_promotion.promote_direct_generation(
        credential=None,
        storage_account="acct",
        db_name="core_nt",
        operation_id="owner",
        generation_id=generation,
        data_prefix=prefix,
        release={},
        transfer_sha=transfer_sha,
        archive_count=1,
        container=container,
    )
    assert repeated["outcome"] == "already_promoted"


def test_recovery_success_survives_lock_release_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "prepare_operation_id": "owner",
        "pending_generation": {
            "id": "ncbi-direct-20260819-aaaaaaaaaaaa",
            "data_prefix": "core_nt/generations/ncbi-direct-20260819-aaaaaaaaaaaa",
            "source_provider": "ncbi-direct",
            "source_release_at": "2026-08-19T00:00:00",
            "release_fingerprint": "b" * 64,
            "transfer_manifest_sha256": "a" * 64,
            "archive_count": 1,
            "number_of_letters": 100,
            "number_of_sequences": 10,
        },
        "aks_job_ref": {"job_name": "prepare-core"},
    }
    monkeypatch.setattr(
        "api.services.ncbi_direct_lock.claim_or_refresh_direct_lock",
        lambda _owner: True,
    )

    def release_failure(_owner: str) -> bool:
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(
        "api.services.ncbi_direct_lock.release_direct_lock",
        release_failure,
    )
    monkeypatch.setattr(
        direct_promotion,
        "promote_direct_generation",
        lambda **_kwargs: {
            "outcome": "promoted",
            "generation_id": "ncbi-direct-20260819-aaaaaaaaaaaa",
            "files_total": 12,
            "bytes_total": 200,
        },
    )

    result = direct_promotion.recover_direct_generation(
        credential=None,
        storage_account="acct",
        db_name="core_nt",
        metadata=metadata,
        container=object(),
    )

    assert result["outcome"] == "promoted"
