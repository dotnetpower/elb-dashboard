"""Tests for prepare-db hardening: lock + stale recovery + copy.status poll.

Responsibility: Verify that prepare_db's daemon-thread pipeline correctly
    aggregates copy.status, refuses to promote source_version on partial
    completion, recovers from stale in-progress markers, and that ETag-aware
    metadata writes survive a concurrent reader/writer.
Edit boundaries: Unit-test the helpers (`_poll_copy_completion`,
    `_is_stale_prepare_marker`, `_update_metadata`) without spinning up the
    full FastAPI app or Azure Storage.
Key entry points: `test_poll_marks_all_success`,
    `test_poll_records_failed_blobs`,
    `test_poll_handles_aborted`, `test_poll_returns_timeout`,
    `test_stale_marker_recovers`, `test_update_metadata_retries_on_etag_clash`.
Risky contracts: ``copy_status.phase == "completed"`` is the SPA's source of
    truth for "this DB is Ready"; the polling helper must only return that
    state when EVERY copy reaches success.
Validation: `uv run pytest -q api/tests/test_prepare_db_hardening.py`.
"""

from __future__ import annotations

import sys as _sys
from datetime import UTC, datetime, timedelta
from typing import Any

# api/routes/storage/__init__.py rebinds the symbol ``prepare_db`` to the route
# *function* (re-exported from this submodule), shadowing the submodule on the
# package object. Reach for the submodule via sys.modules so module-level
# constants like ``_COPY_POLL_INTERVAL_SECONDS`` resolve correctly.
import api.routes.storage.prepare_db  # noqa: F401 — ensure submodule is imported
import pytest
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

prepare_db_module = _sys.modules["api.routes.storage.prepare_db"]


class _FakeCopyProps:
    def __init__(self, status: str, description: str = "") -> None:
        self.copy = type("_Copy", (), {"status": status, "status_description": description})


class _FakeBlobClient:
    def __init__(self, status: str, description: str = "") -> None:
        self._props = _FakeCopyProps(status, description)

    def get_blob_properties(self) -> _FakeCopyProps:
        return self._props


class _FakeBlobItem:
    def __init__(self, name: str, status: str, description: str = "") -> None:
        self.name = name
        self.copy = type("_Copy", (), {"status": status, "status_description": description})


class _FakeContainerClient:
    def __init__(self, statuses: dict[str, tuple[str, str]]) -> None:
        self._statuses = statuses

    def get_blob_client(self, name: str) -> _FakeBlobClient:
        status, description = self._statuses.get(name, ("success", ""))
        return _FakeBlobClient(status, description)

    def list_blobs(self, *, name_starts_with: str = "", include: Any = None) -> list[_FakeBlobItem]:
        del include
        return [
            _FakeBlobItem(name, status, description)
            for name, (status, description) in self._statuses.items()
            if name.startswith(name_starts_with)
        ]


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop the inter-batch sleep so unit tests don't actually wait. The poller
    # (and its `_COPY_POLL_*` constants) now lives in the copy_poller service
    # module, so the patch targets that module rather than the route facade.
    from api.services.storage import prepare_db_copy_poller as _copy_poller

    monkeypatch.setattr(_copy_poller, "_COPY_POLL_INTERVAL_SECONDS", 0.0, raising=True)


def test_poll_marks_all_success() -> None:
    container = _FakeContainerClient(
        {
            "core_nt/a": ("success", ""),
            "core_nt/b": ("success", ""),
        }
    )
    out = prepare_db_module._poll_copy_completion(
        container, ["core_nt/a", "core_nt/b"], db_name="core_nt"
    )
    assert out["success"] == 2
    assert out["failed"] == 0
    assert out["aborted"] == 0
    assert out["timed_out"] is False
    assert out["pending"] == 0
    assert out["failed_files"] == []


def test_poll_records_failed_blobs() -> None:
    container = _FakeContainerClient(
        {
            "swissprot/a": ("success", ""),
            "swissprot/b": ("failed", "source 404"),
            "swissprot/c": ("aborted", "client cancelled"),
        }
    )
    out = prepare_db_module._poll_copy_completion(
        container,
        ["swissprot/a", "swissprot/b", "swissprot/c"],
        db_name="swissprot",
    )
    assert out["success"] == 1
    assert out["failed"] == 1
    assert out["aborted"] == 1
    statuses = {f["status"] for f in out["failed_files"]}
    assert statuses == {"failed", "aborted"}


def test_poll_returns_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # All blobs stay "pending" — capped runtime forces a timeout result.
    container = _FakeContainerClient(
        {
            "nt/a": ("pending", ""),
        }
    )
    from api.services.storage import prepare_db_copy_poller as _copy_poller

    monkeypatch.setattr(_copy_poller, "_COPY_POLL_MAX_SECONDS", 0.0, raising=True)
    out = prepare_db_module._poll_copy_completion(container, ["nt/a"], db_name="nt")
    assert out["timed_out"] is True
    assert out["pending"] >= 1


def test_poll_falls_back_when_list_copy_include_is_unsupported() -> None:
    class _FallbackContainer(_FakeContainerClient):
        def list_blobs(self, *, name_starts_with: str = "", include: Any = None) -> list[Any]:
            if include is not None:
                raise TypeError("include is not supported")
            return [
                type("_Listed", (), {"name": name})()
                for name in self._statuses
                if name.startswith(name_starts_with)
            ]

    container = _FallbackContainer(
        {
            "swissprot/a": ("success", ""),
            "swissprot/b": ("failed", "source 404"),
        }
    )
    out = prepare_db_module._poll_copy_completion(
        container,
        ["swissprot/a", "swissprot/b"],
        db_name="swissprot",
    )
    assert out["success"] == 1
    assert out["failed"] == 1
    assert out["timed_out"] is False


def test_poll_propagates_operation_ownership_loss() -> None:
    from api.services.storage.prepare_db_metadata import (
        DatabaseOperationOwnershipError,
    )

    container = _FakeContainerClient({"core_nt/a": ("pending", "")})

    def _ownership_lost(_snapshot: dict[str, int]) -> None:
        raise DatabaseOperationOwnershipError("ownership changed")

    with pytest.raises(DatabaseOperationOwnershipError, match="ownership changed"):
        prepare_db_module._poll_copy_completion(
            container,
            ["core_nt/a"],
            db_name="core_nt",
            on_progress=_ownership_lost,
        )


def test_stale_marker_recovers() -> None:
    from api.services.storage import prepare_db_metadata as _metadata

    fresh = datetime.now(UTC).isoformat()
    old = (
        datetime.now(UTC) - timedelta(seconds=_metadata._PREPARE_DB_STALE_SECONDS + 60)
    ).isoformat()
    assert (
        prepare_db_module._is_stale_prepare_marker(
            {"update_in_progress": True, "update_started_at": fresh}
        )
        is False
    )
    assert (
        prepare_db_module._is_stale_prepare_marker(
            {"update_in_progress": True, "update_started_at": old}
        )
        is True
    )
    # Missing flag = effectively stale (allow new daemon).
    assert prepare_db_module._is_stale_prepare_marker({}) is True


def test_stale_sharding_marker_recovers() -> None:
    from api.services.storage import prepare_db_metadata as metadata_module

    fresh = datetime.now(UTC).isoformat()
    old = (
        datetime.now(UTC) - timedelta(seconds=metadata_module._SHARDING_STALE_SECONDS + 60)
    ).isoformat()

    assert metadata_module.is_stale_sharding_marker({}) is True
    assert (
        metadata_module.is_stale_sharding_marker(
            {"sharding_in_progress": True, "sharding_started_at": fresh}
        )
        is False
    )
    assert (
        metadata_module.is_stale_sharding_marker(
            {"sharding_in_progress": True, "sharding_started_at": old}
        )
        is True
    )


def test_prepare_stale_window_exceeds_server_copy_poll_ceiling() -> None:
    from api.services.storage import prepare_db_metadata as metadata_module
    from api.services.storage.prepare_db_copy_poller import _COPY_POLL_MAX_SECONDS

    assert metadata_module._PREPARE_DB_STALE_SECONDS > _COPY_POLL_MAX_SECONDS


def test_sharding_stale_window_exceeds_warmup_task_limit() -> None:
    from api.services.storage import prepare_db_metadata as metadata_module
    from api.tasks.storage import warmup as warmup_module

    assert metadata_module._SHARDING_STALE_SECONDS > warmup_module._TASK_HARD_TIME_LIMIT


def test_prepare_operation_owner_rejects_superseded_daemon() -> None:
    from api.services.storage import prepare_db_metadata as metadata_module

    metadata_module.require_prepare_operation_owner(
        {"update_in_progress": True, "prepare_operation_id": "owner-a"},
        "owner-a",
    )
    metadata_module.require_prepare_operation_owner({"update_in_progress": True}, "")

    with pytest.raises(
        metadata_module.DatabaseOperationOwnershipError,
        match="ownership changed",
    ):
        metadata_module.require_prepare_operation_owner(
            {"update_in_progress": True, "prepare_operation_id": "owner-b"},
            "owner-a",
        )

    for metadata in ({}, {"update_in_progress": True, "prepare_operation_id": "owner-a"}):
        with pytest.raises(
            metadata_module.DatabaseOperationOwnershipError,
            match="ownership changed",
        ):
            metadata_module.require_prepare_operation_owner(metadata, "")


@pytest.mark.parametrize("max_attempts", [True, 0, 11])
def test_update_metadata_rejects_invalid_retry_bound(max_attempts: Any) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        prepare_db_module._update_metadata(
            object(),
            "core_nt",
            "stacc",
            lambda meta: meta,
            max_attempts=max_attempts,
        )


def test_update_metadata_retries_on_etag_clash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two writers race, the ETag retry loop must converge instead of
    losing the mutation. We simulate one ResourceModifiedError, then success."""

    state: dict[str, Any] = {"meta": {"db_name": "core_nt"}, "etag": "etag-1", "attempt": 0}

    class _Stream:
        def __init__(self, data: bytes, etag: str) -> None:
            self._data = data
            self.properties = type("_P", (), {"etag": etag})

        def readall(self) -> bytes:
            return self._data

    class _Blob:
        def download_blob(self, *, offset: int = 0, length: int | None = None) -> _Stream:
            del offset, length
            import json as _json

            return _Stream(_json.dumps(state["meta"]).encode("utf-8"), state["etag"])

        def upload_blob(self, body: bytes, **kwargs: Any) -> dict[str, str]:
            import json as _json

            state["attempt"] += 1
            if state["attempt"] == 1 and kwargs.get("etag") == "etag-1":
                # Simulate a concurrent peer bumping the ETag.
                state["etag"] = "etag-2"
                raise ResourceModifiedError("412")
            state["meta"] = _json.loads(body.decode("utf-8"))
            state["etag"] = "etag-3"
            return {"etag": '"etag-3"'}

    class _Container:
        def get_blob_client(self, _name: str) -> _Blob:
            return _Blob()

    container = _Container()
    monkeypatch.setattr(
        prepare_db_module,
        "notify_blast_db_metadata_changed",
        lambda *_a, **_kw: None,
        raising=False,
    )

    def _mutator(meta: dict[str, Any]) -> dict[str, Any]:
        meta["source_version"] = "2026-05-21-01-05-02"
        return meta

    result = prepare_db_module._update_metadata(container, "core_nt", "stacc", _mutator)
    assert result["source_version"] == "2026-05-21-01-05-02"
    assert state["meta"]["source_version"] == "2026-05-21-01-05-02"
    assert state["attempt"] >= 2


def test_update_metadata_fails_closed_after_etag_retries() -> None:
    """Exhausted optimistic-concurrency retries must never blind-overwrite."""

    state = {"conditional_attempts": 0, "blind_attempts": 0}

    class _Stream:
        properties = type("_P", (), {"etag": "etag-current"})

        def readall(self) -> bytes:
            return b'{"db_name": "core_nt", "source_version": "current"}'

    class _Blob:
        def download_blob(self, *, offset: int = 0, length: int | None = None) -> _Stream:
            del offset, length
            return _Stream()

        def upload_blob(self, _body: bytes, **kwargs: Any) -> dict[str, str]:
            if kwargs.get("etag"):
                state["conditional_attempts"] += 1
                raise ResourceModifiedError("412")
            state["blind_attempts"] += 1
            return {"etag": '"etag-blind"'}

    class _Container:
        def get_blob_client(self, _name: str) -> _Blob:
            return _Blob()

    with pytest.raises(ResourceModifiedError):
        prepare_db_module._update_metadata(
            _Container(),
            "core_nt",
            "stacc",
            lambda meta: {**meta, "sharding_in_progress": True},
            max_attempts=3,
        )

    assert state == {"conditional_attempts": 3, "blind_attempts": 0}


def test_update_metadata_propagates_read_failure_without_writing() -> None:
    state = {"uploads": 0}

    class _Blob:
        def download_blob(self, *, offset: int = 0, length: int | None = None) -> Any:
            del offset, length
            raise RuntimeError("storage read unavailable")

        def upload_blob(self, _body: bytes, **_kwargs: Any) -> dict[str, str]:
            state["uploads"] += 1
            return {"etag": '"unexpected"'}

    class _Container:
        def get_blob_client(self, _name: str) -> _Blob:
            return _Blob()

    with pytest.raises(RuntimeError, match="storage read unavailable"):
        prepare_db_module._update_metadata(
            _Container(),
            "core_nt",
            "stacc",
            lambda meta: {**meta, "sharding_in_progress": True},
        )

    assert state["uploads"] == 0


def test_update_metadata_retries_concurrent_create_without_overwrite() -> None:
    state: dict[str, Any] = {
        "exists": False,
        "etag": "",
        "meta": {},
        "create_attempts": 0,
        "conditional_attempts": 0,
    }

    class _Stream:
        def __init__(self) -> None:
            self.properties = type("_P", (), {"etag": state["etag"]})

        def readall(self) -> bytes:
            import json as _json

            return _json.dumps(state["meta"]).encode("utf-8")

    class _Blob:
        def download_blob(self, *, offset: int = 0, length: int | None = None) -> _Stream:
            del offset, length
            if not state["exists"]:
                raise ResourceNotFoundError("404")
            return _Stream()

        def upload_blob(self, body: bytes, **kwargs: Any) -> dict[str, str]:
            import json as _json

            if kwargs.get("overwrite") is False:
                state["create_attempts"] += 1
                state["exists"] = True
                state["etag"] = "etag-peer"
                state["meta"] = {"db_name": "core_nt", "peer_field": "preserved"}
                raise ResourceExistsError("409")
            state["conditional_attempts"] += 1
            assert kwargs["etag"] == "etag-peer"
            state["meta"] = _json.loads(body.decode("utf-8"))
            state["etag"] = "etag-final"
            return {"etag": '"etag-final"'}

    class _Container:
        def get_blob_client(self, _name: str) -> _Blob:
            return _Blob()

    result = prepare_db_module._update_metadata(
        _Container(),
        "core_nt",
        "stacc",
        lambda meta: {**meta, "sharding_in_progress": True},
    )

    assert result["peer_field"] == "preserved"
    assert result["sharding_in_progress"] is True
    assert state["create_attempts"] == 1
    assert state["conditional_attempts"] == 1
