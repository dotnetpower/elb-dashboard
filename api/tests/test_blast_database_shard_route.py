"""Tests for manual BLAST database shard operation ownership.

Responsibility: Cover the manual shard route's metadata claim, daemon
    publication guards, and lock cleanup without Azure or background threads.
Edit boundaries: Patch Storage, credentials, audit, and thread creation only;
    shard generation details remain covered by `test_db_sharding.py`.
Key entry points: `blast_database_shard` route regressions.
Risky contracts: A prepare operation blocks sharding, only a complete preset
    summary may publish, and a superseded daemon must not clear a peer marker.
Validation: `uv run pytest -q api/tests/test_blast_database_shard_route.py`.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any, ClassVar

import pytest
from api.auth import CallerIdentity
from fastapi import HTTPException

shard_route = importlib.import_module("api.routes.blast.databases_shard")


class _FakeContainer:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = dict(metadata)


class _FakeBlobService:
    def __init__(self, container: _FakeContainer) -> None:
        self.container = container

    def get_container_client(self, _name: str) -> _FakeContainer:
        return self.container


class _CapturedThread:
    targets: ClassVar[list[Any]] = []

    def __init__(self, *, target: Any, **_kwargs: Any) -> None:
        self.target = target

    def start(self) -> None:
        self.targets.append(self.target)


@pytest.fixture(autouse=True)
def _reset_locks_and_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.storage import prepare_db_locks

    with prepare_db_locks._PREPARE_DB_LOCK_REGISTRY_GUARD:
        prepare_db_locks._PREPARE_DB_LOCK_REGISTRY.clear()
    _CapturedThread.targets.clear()
    monkeypatch.setattr(threading, "Thread", _CapturedThread)
    monkeypatch.setattr(
        shard_route,
        "_maybe_open_local_storage_access",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.db.ops_audit.record_db_op",
        lambda **_kwargs: "",
        raising=False,
    )
    monkeypatch.setattr(
        "api.services.blast.db_metadata.notify_blast_db_metadata_changed",
        lambda *_args, **_kwargs: None,
        raising=False,
    )


def _install_storage(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any],
) -> _FakeContainer:
    container = _FakeContainer(metadata)
    service = _FakeBlobService(container)

    def _update_metadata(
        target: _FakeContainer,
        _db_name: str,
        _account_name: str,
        mutator: Any,
    ) -> dict[str, Any]:
        updated = mutator(dict(target.metadata))
        target.metadata = updated
        return updated

    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda *_args, **_kwargs: service,
    )
    monkeypatch.setattr(
        "api.services.storage.prepare_db_metadata.update_metadata",
        _update_metadata,
    )
    return container


def _body() -> dict[str, str]:
    return {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "resource_group": "rg-workload",
        "account_name": "stworkload",
    }


def _caller() -> CallerIdentity:
    return CallerIdentity(
        object_id="00000000-0000-0000-0000-000000000002",
        tenant_id="00000000-0000-0000-0000-000000000003",
        upn="researcher@example.com",
        raw_token="",
        claims={},
    )


def _complete_reconcile() -> dict[str, Any]:
    return {
        "status": "healed",
        "resharded": True,
        "shard": {
            "layout_schema": 1,
            "total_volumes": 2,
            "shard_sets": [1, 2],
            "errors": [],
        },
    }


def test_manual_shard_rejects_live_prepare_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(
        monkeypatch,
        {
            "db_name": "core_nt",
            "source_version": "v1",
            "update_in_progress": True,
        },
    )

    with pytest.raises(HTTPException) as raised:
        shard_route.blast_database_shard("core_nt", _body(), _caller())

    assert raised.value.status_code == 409
    assert _CapturedThread.targets == []
    from api.services.storage.prepare_db_locks import prepare_db_lock

    lock = prepare_db_lock("stworkload", "core_nt")
    assert lock.acquire(blocking=False)
    lock.release()



def test_manual_shard_incomplete_summary_records_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _install_storage(
        monkeypatch,
        {
            "db_name": "core_nt",
            "source_version": "v1",
            "sharded": True,
            "shard_sets": [1, 2],
            "shard_layout_schema": 1,
            "shard_source_version": "v1",
        },
    )
    monkeypatch.setattr(
        "api.services.db.consistency.reconcile_db_consistency",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "resharded": True,
            "shard": {
                "layout_schema": 1,
                "total_volumes": 4,
                "shard_sets": [1, 2],
                "errors": [{"num_shards": 3, "error": "upload failed"}],
            },
        },
    )

    response = shard_route.blast_database_shard("core_nt", _body(), _caller())
    assert response["accepted"] is True
    _CapturedThread.targets.pop()()

    assert container.metadata["sharding_in_progress"] is False
    assert "sharding_operation_id" not in container.metadata
    assert "incomplete shard layout publication" in container.metadata["sharding_error"]
    assert container.metadata["sharded"] is False
    assert container.metadata["shard_sets"] == []
    assert "shard_layout_schema" not in container.metadata
    assert container.metadata["shard_source_version"] is None


def test_manual_shard_source_change_prevents_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _install_storage(
        monkeypatch,
        {"db_name": "core_nt", "source_version": "v1"},
    )

    def _reconcile(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        container.metadata["source_version"] = "v2"
        return _complete_reconcile()

    monkeypatch.setattr(
        "api.services.db.consistency.reconcile_db_consistency",
        _reconcile,
    )

    shard_route.blast_database_shard("core_nt", _body(), _caller())
    _CapturedThread.targets.pop()()

    assert container.metadata["source_version"] == "v2"
    assert container.metadata["sharding_in_progress"] is False
    assert "source version changed" in container.metadata["sharding_error"]
    assert container.metadata.get("shard_source_version") != "v2"


def test_manual_shard_owner_change_preserves_peer_marker_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _install_storage(
        monkeypatch,
        {"db_name": "core_nt", "source_version": "v1"},
    )

    def _reconcile(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        container.metadata["sharding_operation_id"] = "peer-owner"
        return _complete_reconcile()

    monkeypatch.setattr(
        "api.services.db.consistency.reconcile_db_consistency",
        _reconcile,
    )

    shard_route.blast_database_shard("core_nt", _body(), _caller())
    _CapturedThread.targets.pop()()

    assert container.metadata["sharding_in_progress"] is True
    assert container.metadata["sharding_operation_id"] == "peer-owner"
    from api.services.storage.prepare_db_locks import prepare_db_lock

    lock = prepare_db_lock("stworkload", "core_nt")
    assert lock.acquire(blocking=False)
    lock.release()


def test_manual_shard_thread_start_failure_rolls_back_marker_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _install_storage(
        monkeypatch,
        {"db_name": "core_nt", "source_version": "v1"},
    )

    class _StartFailureThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading, "Thread", _StartFailureThread)

    with pytest.raises(HTTPException) as raised:
        shard_route.blast_database_shard("core_nt", _body(), _caller())

    assert raised.value.status_code == 502
    assert container.metadata["sharding_in_progress"] is False
    assert "sharding_operation_id" not in container.metadata
    assert "failed to start" in container.metadata["sharding_error"]
    from api.services.storage.prepare_db_locks import prepare_db_lock

    lock = prepare_db_lock("stworkload", "core_nt")
    assert lock.acquire(blocking=False)
    lock.release()
