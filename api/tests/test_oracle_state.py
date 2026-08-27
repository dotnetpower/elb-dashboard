"""Tests for durable DB order-oracle state transitions.

Responsibility: Verify create-only claims, ETag-guarded updates, owner checks,
    current-ready promotion, and failure cleanup without Azure access.
Edit boundaries: In-memory Blob fakes only; readiness and Kubernetes behavior
    belong to their focused tests.
Key entry points: `test_claim_adopts_same_identity_and_rejects_other`,
    `test_stale_owner_cannot_update_or_release`,
    `test_promote_publishes_ready_then_releases_active`,
    `test_failed_run_does_not_replace_current_ready`.
Risky contracts: Fakes enforce the same `overwrite=False` and If-Match behavior
    required from Azure Blob Storage.
Validation: `uv run pytest -q api/tests/test_oracle_state.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from api.services.db import oracle_state
from api.services.db.oracle_state import (
    OracleBuildInProgress,
    OracleBuildOwnershipLost,
    claim_oracle_build,
    claim_oracle_execution,
    fail_oracle_run,
    promote_oracle_run,
    read_oracle_active,
    read_oracle_current,
    read_oracle_run,
    release_oracle_active,
    update_oracle_run,
)
from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)


class _Stream:
    def __init__(self, data: bytes, etag: str) -> None:
        self._data = data
        self.properties = SimpleNamespace(etag=etag)

    def readall(self) -> bytes:
        return self._data


class _Blob:
    def __init__(self, container: _Container, path: str) -> None:
        self.container = container
        self.path = path

    def download_blob(self, **_kwargs: Any) -> _Stream:
        if self.path not in self.container.rows:
            raise ResourceNotFoundError("missing")
        data, etag = self.container.rows[self.path]
        return _Stream(data, etag)

    def get_blob_properties(self) -> Any:
        if self.path not in self.container.rows:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(etag=self.container.rows[self.path][1])

    def upload_blob(self, data: bytes, **kwargs: Any) -> Any:
        current = self.container.rows.get(self.path)
        if kwargs.get("overwrite") is False and current is not None:
            raise ResourceExistsError("exists")
        if kwargs.get("match_condition") == MatchConditions.IfNotModified:
            if current is None or kwargs.get("etag") != current[1]:
                raise ResourceModifiedError("etag changed")
        self.container.counter += 1
        etag = f"etag-{self.container.counter}"
        self.container.rows[self.path] = (bytes(data), etag)
        return SimpleNamespace(etag=etag)

    def delete_blob(self, **kwargs: Any) -> None:
        current = self.container.rows.get(self.path)
        if current is None:
            raise ResourceNotFoundError("missing")
        if kwargs.get("match_condition") == MatchConditions.IfNotModified and (
            kwargs.get("etag") != current[1]
        ):
            raise ResourceModifiedError("etag changed")
        del self.container.rows[self.path]


class _Container:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[bytes, str]] = {}
        self.counter = 0

    def get_blob_client(self, path: str) -> _Blob:
        return _Blob(self, path)


def _claim(*, identity: str = "identity-1", run_id: str = "run-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "db_name": "core_nt",
        "identity": identity,
        "layout_fingerprint": "layout-1",
        "source_version": "v1",
        "run_id": run_id,
        "owner_operation_id": f"owner-{run_id}",
        "status": "queued",
        "phase": "queued",
        "expected_parts": 2,
        "expected_shards": ["00", "01"],
        "part_prefix": f"metadata/oracles/core_nt/parts/{run_id}/",
        "dispatch_token": "dispatch-1",
    }


def test_claim_adopts_same_identity_and_rejects_other() -> None:
    container = _Container()
    first = _claim()

    assert claim_oracle_build(container, db_name="core_nt", document=first).outcome == "created"
    adopted = claim_oracle_build(
        container,
        db_name="core_nt",
        document=_claim(identity="identity-1", run_id="run-2"),
    )

    assert adopted.outcome == "adopted"
    assert adopted.document["run_id"] == "run-1"
    with pytest.raises(OracleBuildInProgress):
        claim_oracle_build(
            container,
            db_name="core_nt",
            document=_claim(identity="identity-2", run_id="run-3"),
        )


def test_claim_returns_ready_for_current_identity() -> None:
    container = _Container()
    current = {**_claim(), "status": "ready"}
    container.get_blob_client("metadata/oracles/core_nt/status.json").upload_blob(
        json.dumps(current).encode(), overwrite=False
    )

    result = claim_oracle_build(container, db_name="core_nt", document=_claim(run_id="run-2"))

    assert result.outcome == "ready"
    assert read_oracle_active(container, "core_nt") is None


def test_stale_owner_cannot_update_or_release() -> None:
    container = _Container()
    claim_oracle_build(container, db_name="core_nt", document=_claim())

    with pytest.raises(OracleBuildOwnershipLost):
        update_oracle_run(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="other-owner",
            updates={"phase": "dispatching"},
        )
    with pytest.raises(OracleBuildOwnershipLost):
        release_oracle_active(
            container,
            db_name="core_nt",
            owner_operation_id="other-owner",
        )


def test_promote_publishes_ready_then_releases_active() -> None:
    container = _Container()
    claim = _claim()
    claim_oracle_build(container, db_name="core_nt", document=claim)

    promoted = promote_oracle_run(
        container,
        db_name="core_nt",
        run_id="run-1",
        owner_operation_id="owner-run-1",
        ready_document={"ready_parts": 2, "finished_at": "2026-08-27T00:01:00Z"},
    )

    assert promoted["status"] == "ready"
    assert read_oracle_current(container, "core_nt")["run_id"] == "run-1"  # type: ignore[index]
    assert read_oracle_run(container, "core_nt", "run-1")["ready_parts"] == 2  # type: ignore[index]
    assert read_oracle_active(container, "core_nt") is None


def test_current_pointer_conflict_does_not_mark_run_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container()
    claim_oracle_build(container, db_name="core_nt", document=_claim())
    original_write = oracle_state._write_document

    def _conflict_current(value, path, document, *, etag=""):
        if path == "metadata/oracles/core_nt/status.json":
            raise ResourceModifiedError("current raced")
        return original_write(value, path, document, etag=etag)

    monkeypatch.setattr(oracle_state, "_write_document", _conflict_current)

    with pytest.raises(oracle_state.OracleStateConflict, match="current pointer"):
        promote_oracle_run(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            ready_document={"ready_parts": 2, "finished_at": "2026-08-27T00:01:00Z"},
        )

    assert read_oracle_run(container, "core_nt", "run-1")["status"] == "queued"  # type: ignore[index]
    assert read_oracle_current(container, "core_nt") is None
    assert read_oracle_active(container, "core_nt") is not None


def test_published_run_history_failure_retains_active_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container()
    claim_oracle_build(container, db_name="core_nt", document=_claim())
    monkeypatch.setattr(
        oracle_state,
        "update_oracle_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ResourceModifiedError("history raced")),
    )

    with pytest.raises(oracle_state.OracleStateConflict, match="recovery pending"):
        promote_oracle_run(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            ready_document={
                "ready_parts": 2,
                "finished_at": "2026-08-27T00:01:00Z",
            },
        )

    assert read_oracle_current(container, "core_nt")["run_id"] == "run-1"  # type: ignore[index]
    assert read_oracle_active(container, "core_nt") is not None


def test_published_active_release_failure_requires_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container()
    claim_oracle_build(container, db_name="core_nt", document=_claim())
    monkeypatch.setattr(
        oracle_state,
        "release_oracle_active",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ResourceModifiedError("active raced")),
    )

    with pytest.raises(oracle_state.OracleStateConflict, match="active release"):
        promote_oracle_run(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            ready_document={
                "ready_parts": 2,
                "finished_at": "2026-08-27T00:01:00Z",
            },
        )

    assert read_oracle_current(container, "core_nt")["run_id"] == "run-1"  # type: ignore[index]
    assert read_oracle_run(container, "core_nt", "run-1")["status"] == "ready"  # type: ignore[index]
    assert read_oracle_active(container, "core_nt") is not None


def test_promotion_rejects_incomplete_ready_document() -> None:
    container = _Container()
    claim_oracle_build(container, db_name="core_nt", document=_claim())
    incomplete_run = {
        **_claim(),
        "identity": "",
    }
    container.get_blob_client("metadata/oracles/core_nt/runs/run-1/status.json").upload_blob(
        json.dumps(incomplete_run).encode(), overwrite=True
    )

    with pytest.raises(ValueError, match="incomplete or inconsistent"):
        promote_oracle_run(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            ready_document={
                "ready_parts": 2,
                "finished_at": "2026-08-27T00:01:00Z",
            },
        )

    assert read_oracle_current(container, "core_nt") is None


def test_failed_run_does_not_replace_current_ready() -> None:
    container = _Container()
    old = {**_claim(identity="old", run_id="old-run"), "status": "ready"}
    container.get_blob_client("metadata/oracles/core_nt/status.json").upload_blob(
        json.dumps(old).encode(), overwrite=False
    )
    new = _claim(identity="new", run_id="new-run")
    claim_oracle_build(container, db_name="core_nt", document=new)

    failed = fail_oracle_run(
        container,
        db_name="core_nt",
        run_id="new-run",
        owner_operation_id="owner-new-run",
        error_code="job_failed",
        error="one shard failed",
        finished_at="2026-08-27T00:02:00Z",
    )

    assert failed["status"] == "failed"
    assert read_oracle_current(container, "core_nt")["run_id"] == "old-run"  # type: ignore[index]
    assert read_oracle_active(container, "core_nt") is None


def test_execution_claim_rejects_stale_and_duplicate_deliveries() -> None:
    container = _Container()
    claim_oracle_build(container, db_name="core_nt", document=_claim())

    assert (
        claim_oracle_execution(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            dispatch_token="stale-token",
            execution_instance_id="execution-stale",
            started_at="2026-08-27T00:00:00Z",
            deadline_at="2026-08-27T00:32:00Z",
        )
        is False
    )
    assert (
        claim_oracle_execution(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            dispatch_token="dispatch-1",
            execution_instance_id="execution-1",
            started_at="2026-08-27T00:00:00Z",
            deadline_at="2026-08-27T00:32:00Z",
        )
        is True
    )
    assert (
        claim_oracle_execution(
            container,
            db_name="core_nt",
            run_id="run-1",
            owner_operation_id="owner-run-1",
            dispatch_token="dispatch-1",
            execution_instance_id="execution-2",
            started_at="2026-08-27T00:00:01Z",
            deadline_at="2026-08-27T00:32:01Z",
        )
        is False
    )
    assert read_oracle_active(container, "core_nt")["execution_instance_id"] == "execution-1"  # type: ignore[index]
