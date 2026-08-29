"""Tests for the orphaned prepare-db reconciler.

Responsibility: Cover the pure ``classify_prepare_db_entry`` decision branches and the
    ``reconcile_orphaned_prepare_db`` orchestrator (live progress refresh, reset write, skip
    paths, and concurrency-race guards) using an in-memory fake Storage container and an
    injectable Job lookup.
Edit boundaries: Test module only. No production code.
Key entry points: pytest test functions.
Risky contracts: The race test asserts a fresh dispatch (changed owner token)
    is NOT clobbered even when timestamps match.
Validation: ``uv run pytest -q api/tests/test_orphan_prepare_db_reconcile.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from api.services.storage.direct_promotion import DirectGenerationIncompleteError
from api.services.storage.orphan_prepare_db import (
    classify_prepare_db_entry,
    reconcile_orphaned_prepare_db,
)
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
STALE = 7200.0


def _candidate_meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "db_name": "nt",
        "update_in_progress": True,
        "update_started_at": NOW.isoformat(),
        "copy_status": {"phase": "copying", "total_files": 4874},
        "aks_job_ref": {
            "job_name": "prepare-db-nt-260602010502",
            "cluster_name": "elb-cluster-02",
            "subscription_id": "sub",
            "resource_group": "rg-elb-cluster",
            "namespace": "default",
        },
    }
    meta.update(overrides)
    return meta


def _direct_candidate_meta(**overrides: Any) -> dict[str, Any]:
    meta = _candidate_meta(
        prepare_operation_id="direct-owner",
        source_version="2026-07-21-01-05-02",
        copy_status={"phase": "completed", "success": 727},
        pending_generation={
            "id": "ncbi-direct-20260819-aaaaaaaaaaaa",
            "phase": "downloading",
            "source_provider": "ncbi-direct",
            "data_prefix": "nt/generations/ncbi-direct-20260819-aaaaaaaaaaaa",
            "transfer_manifest_sha256": "a" * 64,
            "archive_count": 2,
            "job_id": "prepare-db-direct-nt-1",
        },
    )
    meta["aks_job_ref"]["source_provider"] = "ncbi-direct"
    meta.update(overrides)
    return meta


# --------------------------------------------------------------------------- #
# Pure classifier branches
# --------------------------------------------------------------------------- #


def test_classify_missing_job_resets() -> None:
    action, reason = classify_prepare_db_entry(
        _candidate_meta(), {"missing": True}, now=NOW, stale_seconds=STALE
    )
    assert action == "reset"
    assert "no longer exists" in reason


def test_classify_failed_job_resets() -> None:
    job = {"missing": False, "conditions": [{"type": "Failed", "status": "True"}]}
    action, reason = classify_prepare_db_entry(_candidate_meta(), job, now=NOW, stale_seconds=STALE)
    assert action == "reset"
    assert "failed" in reason


def test_classify_running_job_skips() -> None:
    job = {"missing": False, "active": 3, "succeeded": 0, "completions": 10, "conditions": []}
    action, _ = classify_prepare_db_entry(_candidate_meta(), job, now=NOW, stale_seconds=STALE)
    assert action == "skip-running"


def test_classify_complete_job_skips() -> None:
    job = {
        "missing": False,
        "active": 0,
        "succeeded": 10,
        "completions": 10,
        "conditions": [{"type": "Complete", "status": "True"}],
    }
    action, _ = classify_prepare_db_entry(_candidate_meta(), job, now=NOW, stale_seconds=STALE)
    assert action == "skip-running"


def test_classify_completed_direct_job_recovers_after_grace() -> None:
    job = {
        "missing": False,
        "active": 0,
        "succeeded": 2,
        "completions": 2,
        "conditions": [{"type": "Complete", "status": "True"}],
        "completion_time": (NOW - timedelta(minutes=5)).isoformat(),
    }

    action, _ = classify_prepare_db_entry(
        _direct_candidate_meta(),
        job,
        now=NOW,
        stale_seconds=STALE,
        direct_recovery_grace_seconds=120,
    )

    assert action == "recover-direct"


def test_classify_completed_direct_job_waits_for_original_worker_grace() -> None:
    job = {
        "missing": False,
        "active": 0,
        "succeeded": 2,
        "completions": 2,
        "conditions": [{"type": "Complete", "status": "True"}],
        "completion_time": (NOW - timedelta(seconds=30)).isoformat(),
    }

    action, _ = classify_prepare_db_entry(
        _direct_candidate_meta(),
        job,
        now=NOW,
        stale_seconds=STALE,
        direct_recovery_grace_seconds=120,
    )

    assert action == "skip-running"


def test_classify_missing_direct_job_verifies_markers_before_reset() -> None:
    action, _ = classify_prepare_db_entry(
        _direct_candidate_meta(),
        {"missing": True},
        now=NOW,
        stale_seconds=STALE,
    )

    assert action == "recover-direct"


def test_classify_job_lookup_unavailable_skips() -> None:
    action, _ = classify_prepare_db_entry(_candidate_meta(), None, now=NOW, stale_seconds=STALE)
    assert action == "skip-error"


def test_classify_no_ref_recent_skips() -> None:
    meta = _candidate_meta(
        aks_job_ref=None,
        update_started_at=(NOW - timedelta(seconds=100)).isoformat(),
    )
    action, _ = classify_prepare_db_entry(meta, None, now=NOW, stale_seconds=STALE)
    assert action == "skip-recent"


def test_classify_no_ref_stale_resets() -> None:
    meta = _candidate_meta(
        aks_job_ref=None,
        update_started_at=(NOW - timedelta(seconds=8000)).isoformat(),
    )
    action, _ = classify_prepare_db_entry(meta, None, now=NOW, stale_seconds=STALE)
    assert action == "reset"


def test_classify_no_ref_unparseable_started_resets() -> None:
    meta = _candidate_meta(aks_job_ref=None, update_started_at="not-a-timestamp")
    action, _ = classify_prepare_db_entry(meta, None, now=NOW, stale_seconds=STALE)
    assert action == "reset"


def test_classify_terminal_phase_skips() -> None:
    for phase in ("completed", "partial", "failed", "cancelled"):
        meta = _candidate_meta(copy_status={"phase": phase})
        action, _ = classify_prepare_db_entry(meta, {"missing": True}, now=NOW, stale_seconds=STALE)
        assert action == "skip-terminal", phase


def test_classify_not_in_progress_skips() -> None:
    meta = _candidate_meta(update_in_progress=False)
    action, _ = classify_prepare_db_entry(meta, {"missing": True}, now=NOW, stale_seconds=STALE)
    assert action == "skip-terminal"


# --------------------------------------------------------------------------- #
# In-memory fake Storage container
# --------------------------------------------------------------------------- #


class _FakeStream:
    def __init__(self, data: bytes, etag: str) -> None:
        self._data = data
        self.properties = SimpleNamespace(etag=etag)

    def readall(self) -> bytes:
        return self._data


class _FakeBlobClient:
    def __init__(self, container: _FakeContainer, name: str) -> None:
        self._c = container
        self._name = name

    def download_blob(self, offset: int = 0, length: int | None = None) -> _FakeStream:
        entry = self._c.store.get(self._name)
        if entry is None:
            raise ResourceNotFoundError(self._name)
        data, etag = entry
        return _FakeStream(data, etag)

    def upload_blob(
        self,
        data: bytes,
        *,
        overwrite: bool = True,
        etag: str | None = None,
        match_condition: Any = None,
    ) -> dict[str, str]:
        self._c.on_upload(self._name, etag)
        entry = self._c.store.get(self._name)
        cur_etag = entry[1] if entry else ""
        if etag and etag != cur_etag:
            raise ResourceModifiedError(self._name)
        new_etag = self._c.next_etag()
        self._c.store[self._name] = (bytes(data), new_etag)
        return {"etag": new_etag}


class _FakeContainer:
    def __init__(self) -> None:
        self.store: dict[str, tuple[bytes, str]] = {}
        self.data_blobs: list[tuple[str, int]] = []
        self._seq = 0

    def next_etag(self) -> str:
        self._seq += 1
        return f"etag-{self._seq}"

    def on_upload(self, name: str, etag: str | None) -> None:  # hook for race test
        pass

    def set_metadata(self, db: str, meta: dict[str, Any]) -> None:
        self.store[f"{db}-metadata.json"] = (
            json.dumps(meta).encode("utf-8"),
            self.next_etag(),
        )

    def add_data_blob(self, name: str, size: int) -> None:
        self.data_blobs.append((name, size))

    def get_blob_client(self, name: str) -> _FakeBlobClient:
        return _FakeBlobClient(self, name)

    def walk_blobs(self, delimiter: str = "/") -> list[SimpleNamespace]:
        out = [SimpleNamespace(name=name) for name in self.store]
        # folder prefixes for data blobs
        prefixes = {name.split("/", 1)[0] + "/" for name, _ in self.data_blobs}
        out.extend(SimpleNamespace(name=p) for p in prefixes)
        return out

    def list_blobs(self, name_starts_with: str | None = None) -> list[SimpleNamespace]:
        if name_starts_with is None:
            return [SimpleNamespace(name=name, size=0) for name in self.store]
        return [
            SimpleNamespace(name=name, size=size)
            for name, size in self.data_blobs
            if name.startswith(name_starts_with)
        ]

    def metadata(self, db: str) -> dict[str, Any]:
        return json.loads(self.store[f"{db}-metadata.json"][0])


# --------------------------------------------------------------------------- #
# Orchestrator integration
# --------------------------------------------------------------------------- #


def test_reconcile_disabled_returns_early() -> None:
    out = reconcile_orphaned_prepare_db(credential=None, enabled=False)
    assert out == {"enabled": False, "reset": [], "scanned": 0}


def test_reconcile_no_storage_account(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("STORAGE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT", "AZURE_BLOB_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    out = reconcile_orphaned_prepare_db(credential=None, storage_account=None, enabled=True)
    assert out["skipped"] == "no-storage-account"


def test_reconcile_missing_job_resets_to_partial() -> None:
    container = _FakeContainer()
    candidate = _candidate_meta()
    candidate["prepare_operation_id"] = "orphan-owner"
    candidate["cancel_operation_id"] = "orphan-cancel"
    candidate["cancel_started_at"] = NOW.isoformat()
    candidate.update(
        {
            "sharded": True,
            "shard_sets": [1, 2],
            "shard_layout_schema": 1,
            "shard_source_version": "old",
            "sharded_at": "2026-05-20T00:00:00+00:00",
        }
    )
    container.set_metadata("nt", candidate)
    container.add_data_blob("nt/file1", 100)
    container.add_data_blob("nt/file2", 200)

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {"missing": True},
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["reset"] == ["nt"]
    meta = container.metadata("nt")
    assert meta["update_in_progress"] is False
    assert "prepare_operation_id" not in meta
    assert "cancel_operation_id" not in meta
    assert "cancel_started_at" not in meta
    assert meta["copy_status"]["phase"] == "partial"
    assert meta["copy_status"]["success"] == 2
    assert meta["copy_status"]["total_files"] == 4874
    assert "aks_job_ref" not in meta
    assert "reconciler" in meta["update_error"]
    assert meta["sharded"] is False
    assert meta["shard_sets"] == []
    assert "shard_layout_schema" not in meta
    assert meta["shard_source_version"] is None
    assert "sharded_at" not in meta


def test_reconcile_missing_direct_job_preserves_active_generation() -> None:
    container = _FakeContainer()
    candidate = _direct_candidate_meta()
    candidate.update(
        {
            "sharded": True,
            "shard_sets": [1, 2],
            "shard_layout_schema": 1,
            "shard_source_version": "2026-07-21-01-05-02",
        }
    )
    container.set_metadata("nt", candidate)

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {"missing": True},
        direct_recover=lambda **_kwargs: (_ for _ in ()).throw(
            DirectGenerationIncompleteError("markers incomplete")
        ),
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["reset"] == ["nt"]
    meta = container.metadata("nt")
    assert meta["update_in_progress"] is False
    assert meta["copy_status"] == {"phase": "completed", "success": 727}
    assert meta["pending_generation"]["phase"] == "partial"
    assert meta["sharded"] is True
    assert meta["shard_sets"] == [1, 2]
    assert meta["shard_source_version"] == "2026-07-21-01-05-02"


def test_reconcile_completed_direct_job_invokes_durable_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _FakeContainer()
    container.set_metadata("nt", _direct_candidate_meta())
    calls: list[dict[str, Any]] = []

    def recover(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "outcome": "promoted",
            "generation_id": "ncbi-direct-20260819-aaaaaaaaaaaa",
            "files_total": 20,
            "bytes_total": 200,
        }

    cleanup_calls: list[dict[str, Any]] = []

    def cleanup(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        cleanup_calls.append(kwargs)
        return {"status": "deleted"}

    monkeypatch.setattr(
        "api.services.k8s.prepare_db_jobs.delete_prepare_db_job",
        cleanup,
    )

    out = reconcile_orphaned_prepare_db(
        credential="credential",
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {
            "missing": False,
            "succeeded": 2,
            "completions": 2,
            "completion_time": (NOW - timedelta(minutes=5)).isoformat(),
            "conditions": [{"type": "Complete", "status": "True"}],
        },
        direct_recover=recover,
        direct_recovery_grace_seconds=120,
        now=NOW,
        stale_seconds=STALE,
    )

    assert calls[0]["credential"] == "credential"
    assert calls[0]["db_name"] == "nt"
    assert cleanup_calls == [
        {
            "namespace": "default",
            "job_name": "prepare-db-nt-260602010502",
            "configmap_name": "prepare-db-nt-260602010502",
        }
    ]
    assert out["recovered_direct"] == [
        {
            "db_name": "nt",
            "job_id": "prepare-db-direct-nt-1",
            "outcome": "promoted",
            "generation_id": "ncbi-direct-20260819-aaaaaaaaaaaa",
            "files_total": 20,
            "bytes_total": 200,
            "aks_cleanup": {"status": "deleted"},
        }
    ]


def test_reconcile_running_job_leaves_row_untouched() -> None:
    container = _FakeContainer()
    container.set_metadata("nt", _candidate_meta())
    before = container.metadata("nt")

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {
            "missing": False,
            "active": 5,
            "succeeded": 0,
            "completions": 10,
            "conditions": [],
        },
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["reset"] == []
    assert out["skipped_running"] == ["nt"]
    assert container.metadata("nt") == before


def test_reconcile_running_direct_job_refreshes_durable_progress() -> None:
    container = _FakeContainer()
    container.set_metadata("nt", _direct_candidate_meta())

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {
            "missing": False,
            "active": 4,
            "succeeded": 3,
            "failed": 1,
            "completions": 10,
            "conditions": [],
        },
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["refreshed_direct"] == ["nt"]
    assert out["skipped_running"] == ["nt"]
    pending = container.metadata("nt")["pending_generation"]
    assert pending["active_pods"] == 4
    assert pending["succeeded_archives"] == 3
    assert pending["failed_pods"] == 1


def test_reconcile_running_direct_progress_does_not_clobber_new_owner() -> None:
    class _RaceContainer(_FakeContainer):
        def __init__(self) -> None:
            super().__init__()
            self._raced = False

        def on_upload(self, name: str, etag: str | None) -> None:
            if name == "nt-metadata.json" and not self._raced:
                self._raced = True
                fresh = _direct_candidate_meta(prepare_operation_id="new-owner")
                fresh["pending_generation"]["id"] = "ncbi-direct-new-generation"
                fresh["aks_job_ref"]["job_name"] = "prepare-db-direct-nt-new"
                self.store[name] = (
                    json.dumps(fresh).encode("utf-8"),
                    self.next_etag(),
                )

    container = _RaceContainer()
    container.set_metadata("nt", _direct_candidate_meta())

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {
            "missing": False,
            "active": 4,
            "succeeded": 3,
            "failed": 0,
            "completions": 10,
            "conditions": [],
        },
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["refreshed_direct"] == []
    assert out["skipped_raced"] == ["nt"]
    meta = container.metadata("nt")
    assert meta["prepare_operation_id"] == "new-owner"
    assert meta["pending_generation"]["id"] == "ncbi-direct-new-generation"
    assert meta["pending_generation"].get("succeeded_archives") is None


def test_reconcile_job_lookup_exception_skips() -> None:
    container = _FakeContainer()
    container.set_metadata("nt", _candidate_meta())
    before = container.metadata("nt")

    def _boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("AKS API unavailable")

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=_boom,
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["reset"] == []
    assert out["skipped_error"] == ["nt"]
    assert container.metadata("nt") == before


def test_reconcile_race_with_fresh_dispatch_is_skipped() -> None:
    """If a fresh dispatch replaces the orphan between read and write (ETag
    collision), the reset mutator must abandon the write rather than clobber
    the new download."""

    class _RaceContainer(_FakeContainer):
        def __init__(self) -> None:
            super().__init__()
            self._raced = False

        def on_upload(self, name: str, etag: str | None) -> None:
            if name == "nt-metadata.json" and not self._raced:
                self._raced = True
                # Simulate a brand-new dispatch with the same timestamp but a
                # new owner + bumped ETag. Timestamp-only recovery would
                # clobber this row on retry.
                fresh = _candidate_meta()
                fresh["prepare_operation_id"] = "new-owner"
                fresh["aks_job_ref"]["job_name"] = "prepare-db-nt-NEWDISPATCH"
                self.store[name] = (
                    json.dumps(fresh).encode("utf-8"),
                    self.next_etag(),
                )

    container = _RaceContainer()
    candidate = _candidate_meta()
    candidate["prepare_operation_id"] = "old-owner"
    container.set_metadata("nt", candidate)

    out = reconcile_orphaned_prepare_db(
        credential=None,
        storage_account="acct",
        container=container,
        job_lookup=lambda *a, **k: {"missing": True},
        now=NOW,
        stale_seconds=STALE,
    )

    assert out["reset"] == []
    assert out["skipped_raced"] == ["nt"]
    # The fresh dispatch's state survived untouched.
    meta = container.metadata("nt")
    assert meta["update_in_progress"] is True
    assert meta["prepare_operation_id"] == "new-owner"
    assert meta["aks_job_ref"]["job_name"] == "prepare-db-nt-NEWDISPATCH"
