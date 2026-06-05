"""Tests for the orphaned prepare-db reconciler.

Responsibility: Cover the pure ``classify_prepare_db_entry`` decision branches and the
    ``reconcile_orphaned_prepare_db`` orchestrator (reset write, skip paths, and the
    concurrency-race guard) using an in-memory fake Storage container and an injectable
    Job lookup.
Edit boundaries: Test module only. No production code.
Key entry points: pytest test functions.
Risky contracts: The race test asserts a fresh dispatch (changed ``update_started_at``)
    is NOT clobbered — keep it green when touching the reset mutator.
Validation: ``uv run pytest -q api/tests/test_orphan_prepare_db_reconcile.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
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
    action, reason = classify_prepare_db_entry(
        _candidate_meta(), job, now=NOW, stale_seconds=STALE
    )
    assert action == "reset"
    assert "failed" in reason


def test_classify_running_job_skips() -> None:
    job = {"missing": False, "active": 3, "succeeded": 0, "completions": 10, "conditions": []}
    action, _ = classify_prepare_db_entry(
        _candidate_meta(), job, now=NOW, stale_seconds=STALE
    )
    assert action == "skip-running"


def test_classify_complete_job_skips() -> None:
    job = {
        "missing": False,
        "active": 0,
        "succeeded": 10,
        "completions": 10,
        "conditions": [{"type": "Complete", "status": "True"}],
    }
    action, _ = classify_prepare_db_entry(
        _candidate_meta(), job, now=NOW, stale_seconds=STALE
    )
    assert action == "skip-running"


def test_classify_job_lookup_unavailable_skips() -> None:
    action, _ = classify_prepare_db_entry(
        _candidate_meta(), None, now=NOW, stale_seconds=STALE
    )
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
        action, _ = classify_prepare_db_entry(
            meta, {"missing": True}, now=NOW, stale_seconds=STALE
        )
        assert action == "skip-terminal", phase


def test_classify_not_in_progress_skips() -> None:
    meta = _candidate_meta(update_in_progress=False)
    action, _ = classify_prepare_db_entry(
        meta, {"missing": True}, now=NOW, stale_seconds=STALE
    )
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
    container.set_metadata("nt", _candidate_meta())
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
    assert meta["copy_status"]["phase"] == "partial"
    assert meta["copy_status"]["success"] == 2
    assert meta["copy_status"]["total_files"] == 4874
    assert "aks_job_ref" not in meta
    assert "reconciler" in meta["update_error"]


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
                # Simulate a brand-new dispatch landing: new started_at + new
                # job ref + bumped ETag so the If-Match upload 412s and the
                # retry re-reads this fresh row.
                fresh = _candidate_meta(
                    update_started_at=(NOW + timedelta(minutes=1)).isoformat(),
                )
                fresh["aks_job_ref"]["job_name"] = "prepare-db-nt-NEWDISPATCH"
                self.store[name] = (
                    json.dumps(fresh).encode("utf-8"),
                    self.next_etag(),
                )

    container = _RaceContainer()
    container.set_metadata("nt", _candidate_meta())

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
    assert meta["aks_job_ref"]["job_name"] == "prepare-db-nt-NEWDISPATCH"
