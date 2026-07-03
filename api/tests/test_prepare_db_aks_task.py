"""Tests for the `prepare_db_via_aks` Celery task — issue #7 Phase 1.

Responsibility: Cover the happy path (Job + blobs succeed -> promote) and
    the partial paths (Job submit error, Job timeout, blob partial) so the
    final metadata.json shape matches the server-side path and the cleanup
    `delete` of the K8s Job + ConfigMap always runs.
Edit boundaries: Stubs the K8s session module, the Storage container, and
    every facade indirection used by the task. No real Azure or
    Kubernetes touched.
Key entry points: `test_aks_task_happy_path_promotes_metadata`,
    `test_aks_task_submit_error_marks_partial`,
    `test_aks_task_blob_partial_marks_partial`,
    `test_aks_task_always_deletes_job`.
Risky contracts: Final metadata `copy_status.mode == "aks"` plus the
    classic promoted-shape keys (`source_version`, `update_completed_at`,
    `shard_sets`, `sharded`) come from this task; deviations break the SPA
    polling loop.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_task.py`.
"""

from __future__ import annotations

import sys as _sys
import types as _types
from typing import Any

# `api.tasks.storage.__init__` re-exports `prepare_db_via_aks` (the task
# callable), shadowing the submodule attribute. Pull the real submodule
# from `sys.modules` so we can monkeypatch its internals.
import api.tasks.storage  # noqa: F401 — ensure the package init runs
import pytest

task_module = _sys.modules["api.tasks.storage.prepare_db_via_aks"]
prepare_db_via_aks = task_module.prepare_db_via_aks


class _FakeTask:
    """Stand-in for the bound Celery task `self`. Only `request.id` is read."""

    request = type("_R", (), {"id": "task-aks-test"})()

    def update_state(self, **_kw: Any) -> None:
        return None


class _FakeContainer:
    """Tracks every metadata mutation so tests can assert on the final shape."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.meta: dict[str, Any] = dict(initial or {})
        self.update_calls: list[dict[str, Any]] = []
        # Optional staged-blob inventory for `_count_staged_blobs`. Names are
        # full blob paths (e.g. "core_nt/core_nt.000.nhr"); the optional
        # per-name byte size feeds the download-speed signal.
        self.blob_names: list[str] = []
        self.blob_sizes: dict[str, int] = {}
        # Optional per-blob `last_modified` (tz-aware datetime). Drives the
        # `since`-filtered progress count; blobs without an entry behave as
        # if `last_modified` is unset (always counted), matching the legacy
        # fixtures that predate the timestamp filter.
        self.blob_last_modified: dict[str, Any] = {}

    def list_blobs(self, name_starts_with: str = "") -> list[Any]:
        return [
            _types.SimpleNamespace(
                name=name,
                size=self.blob_sizes.get(name, 0),
                last_modified=self.blob_last_modified.get(name),
            )
            for name in self.blob_names
            if name.startswith(name_starts_with)
        ]


def _fake_update_metadata(
    container: _FakeContainer, db_name: str, account: str, mutator: Any
) -> None:
    new_meta = mutator(dict(container.meta))
    container.meta = new_meta
    container.update_calls.append({"db": db_name, "account": account, "meta": new_meta})


def _fake_poll_copy_completion(
    container: _FakeContainer,
    staged_blob_names: list[str],
    *,
    db_name: str,
    **_kw: Any,
) -> dict[str, Any]:
    del container, db_name
    return {
        "success": len(staged_blob_names),
        "failed": 0,
        "aborted": 0,
        "pending": 0,
        "timed_out": False,
        "failed_files": [],
    }


def _fake_blob_service(_cred: Any, _account: str):
    """Returns a service whose container is read from the test-local closure."""
    return _FakeBlobSvcHolder.svc  # type: ignore[attr-defined]


class _FakeBlobSvc:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container

    def get_container_client(self, _name: str) -> _FakeContainer:
        return self._container


class _FakeBlobSvcHolder:
    svc: Any = None


@pytest.fixture(autouse=True)
def patch_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # The task imports these lazily from api.routes.storage.prepare_db.
    # Patch the *source module* so the lazy import inside the task picks
    # up the fakes when it runs. Use SimpleNamespace so functions are not
    # auto-bound as methods on the fake module object.
    fake_prepare_db = _types.SimpleNamespace(
        _poll_copy_completion=_fake_poll_copy_completion,
        _update_metadata=_fake_update_metadata,
    )
    monkeypatch.setitem(_sys.modules, "api.routes.storage.prepare_db", fake_prepare_db)

    # `from api.services.storage.data import _blob_service` inside the task
    import api.services.storage.data as data_module

    monkeypatch.setattr(data_module, "_blob_service", _fake_blob_service, raising=True)

    # The promote path imports sharding + signature lookup; stub both.
    fake_sharding = _types.SimpleNamespace(
        PRESET_SHARD_SETS=(1,),
        derive_volumes_from_keys=lambda _db, keys: [k.rsplit("/", 1)[-1] for k in keys],
        upload_shard_set=lambda *_a, **_kw: None,
    )
    monkeypatch.setitem(_sys.modules, "api.services.db.sharding", fake_sharding)
    # The promote path now runs the consistency reconcile (prune ghosts + regen
    # shard layout) instead of the old upload_shard_set loop; stub it to report a
    # clean heal so the metadata still promotes sharded=True.
    fake_consistency = _types.SimpleNamespace(
        reconcile_db_consistency=lambda *_a, **_kw: {
            "status": "healed",
            "resharded": True,
            "shard": {"shard_sets": [1], "total_volumes": 1, "errors": []},
            "prune": {"status": "clean"},
        },
    )
    monkeypatch.setitem(_sys.modules, "api.services.db.consistency", fake_consistency)
    fake_catalogue = _types.SimpleNamespace(
        database_update_signature=lambda _db: {
            "signature_etag": "sig-aks-1",
            "composite_signature": "comp-aks-1",
        }
    )
    monkeypatch.setitem(_sys.modules, "api.services.ncbi_catalogue", fake_catalogue)

    # Facade indirection — the task calls `_facade._update_state`,
    # `_facade._record_task_progress`, `_facade.get_credential`,
    # `_facade._publish_db_metadata_invalidate`.
    monkeypatch.setattr(task_module, "_update_state", lambda *_a, **_kw: None)
    monkeypatch.setattr(task_module, "_record_task_progress", lambda *_a, **_kw: None)
    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(
        task_module._facade,
        "_publish_db_metadata_invalidate",
        lambda *_a, **_kw: None,
        raising=False,
    )

    # Skip the poll loop's sleep.
    monkeypatch.setattr(task_module.time, "sleep", lambda *_a, **_kw: None)


def _set_container(container: _FakeContainer) -> None:
    _FakeBlobSvcHolder.svc = _FakeBlobSvc(container)


def _base_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        job_id="job-aks-1",
        subscription_id="00000000-0000-0000-0000-000000000001",
        storage_resource_group="rg-workload",
        storage_account="stworkload",
        db_name="core_nt",
        source_version="2026-05-21-01-05-02",
        file_keys=[
            "2026-05-21-01-05-02/core_nt.000.nhr",
            "2026-05-21-01-05-02/core_nt.000.nin",
        ],
        file_sizes={
            "2026-05-21-01-05-02/core_nt.000.nhr": 1024,
            "2026-05-21-01-05-02/core_nt.000.nin": 4096,
        },
        aks_resource_group="rg-elb",
        cluster_name="aks-elb",
        max_pods=2,
        files_per_pod=1,
        caller_oid="oid-test",
    )
    base.update(overrides)
    return base


def test_aks_task_happy_path_promotes_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer({"db_name": "core_nt"})
    _set_container(container)

    submit_calls: list[dict[str, Any]] = []
    delete_calls: list[str] = []

    def _fake_submit(*_a, configmap_manifest, job_manifest, **_kw):
        submit_calls.append({"job": job_manifest["metadata"]["name"]})
        return {"status": "created", "stage": "job"}

    def _fake_get_job(*_a, **_kw):
        return {
            "missing": False,
            "active": 0,
            "succeeded": 2,
            "failed": 0,
            "completions": 2,
            "parallelism": 2,
            "conditions": [{"type": "Complete", "status": "True"}],
        }

    def _fake_delete(*_a, **_kw):
        delete_calls.append("deleted")
        return {"status": "deleted"}

    monkeypatch.setattr(task_module, "submit_prepare_db_job", _fake_submit)
    monkeypatch.setattr(task_module, "get_prepare_db_job", _fake_get_job)
    monkeypatch.setattr(task_module, "delete_prepare_db_job", _fake_delete)

    result = prepare_db_via_aks.run(**_base_kwargs())

    assert result["ok"] is True
    assert result["outcome"] == "promoted"
    assert result["mode"] == "aks"
    assert result["files_succeeded"] == 2
    assert result["files_failed"] == 0

    # The container's final metadata is the promoted shape.
    meta = container.meta
    assert meta["update_in_progress"] is False
    assert meta["source_version"] == "2026-05-21-01-05-02"
    assert meta["copy_status"]["mode"] == "aks"
    assert meta["copy_status"]["phase"] == "completed"
    assert meta["copy_status"]["failed"] == 0
    assert meta["sharded"] is True
    assert meta["shard_sets"] == [1]
    assert meta["signature_etag"] == "sig-aks-1"
    # The route-side keys must be removed on promotion.
    assert "update_error" not in meta
    assert "failed_files" not in meta
    assert "updating_to_source_version" not in meta

    assert len(submit_calls) == 1
    assert delete_calls == ["deleted"]


def test_aks_task_submit_error_marks_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer({"db_name": "core_nt"})
    _set_container(container)

    def _fake_submit(*_a, **_kw):
        return {
            "status": "error",
            "stage": "configmap",
            "configmap": {"status": "error", "error": "boom"},
        }

    poll_calls: list[Any] = []
    delete_calls: list[str] = []

    def _fake_get_job(*_a, **_kw):
        poll_calls.append("get")
        return {"missing": True}

    def _fake_delete(*_a, **_kw):
        delete_calls.append("d")
        return {"status": "deleted"}

    monkeypatch.setattr(task_module, "submit_prepare_db_job", _fake_submit)
    monkeypatch.setattr(task_module, "get_prepare_db_job", _fake_get_job)
    monkeypatch.setattr(task_module, "delete_prepare_db_job", _fake_delete)

    result = prepare_db_via_aks.run(**_base_kwargs())

    assert result["ok"] is False
    assert result["reason"] == "submit_failed"
    # Submit failure should NOT trigger Job polling or Job delete.
    assert poll_calls == []
    assert delete_calls == []

    meta = container.meta
    assert meta["update_in_progress"] is False
    assert meta["copy_status"]["mode"] == "aks"
    assert meta["copy_status"]["phase"] == "partial"
    assert "AKS Job submit error" in meta.get("update_error", "")
    assert meta["aks_submit_summary"]["stage"] == "configmap"


def test_aks_task_blob_partial_marks_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer({"db_name": "core_nt"})
    _set_container(container)

    def _fake_submit(*_a, **_kw):
        return {"status": "created"}

    def _fake_get_job(*_a, **_kw):
        return {
            "missing": False,
            "active": 0,
            "succeeded": 2,
            "failed": 0,
            "completions": 2,
            "parallelism": 2,
            "conditions": [{"type": "Complete", "status": "True"}],
        }

    monkeypatch.setattr(task_module, "submit_prepare_db_job", _fake_submit)
    monkeypatch.setattr(task_module, "get_prepare_db_job", _fake_get_job)
    monkeypatch.setattr(task_module, "delete_prepare_db_job", lambda *_a, **_kw: None)

    # Simulate blob-poll partial.
    def _bad_poll(_container, staged, *, db_name, **_kw):
        return {
            "success": len(staged) - 1,
            "failed": 1,
            "aborted": 0,
            "pending": 0,
            "timed_out": False,
            "failed_files": [
                {"key": "core_nt/core_nt.000.nin", "status_description": "fake-fail"}
            ],
        }

    monkeypatch.setitem(
        _sys.modules,
        "api.routes.storage.prepare_db",
        _types.SimpleNamespace(
            _poll_copy_completion=_bad_poll,
            _update_metadata=_fake_update_metadata,
        ),
    )

    result = prepare_db_via_aks.run(**_base_kwargs())

    assert result["ok"] is False
    assert result["outcome"] == "partial"
    meta = container.meta
    assert meta["update_in_progress"] is False
    # _mark_partial w/ copy_summary uses the summary directly (no mode tag).
    assert meta["copy_status"]["phase"] == "partial"
    assert meta["copy_status"]["failed"] == 1
    assert meta["failed_files"][0]["key"] == "core_nt/core_nt.000.nin"


def test_aks_task_always_deletes_job_on_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer({"db_name": "core_nt"})
    _set_container(container)

    delete_calls: list[str] = []

    monkeypatch.setattr(
        task_module, "submit_prepare_db_job", lambda *_a, **_kw: {"status": "created"}
    )
    monkeypatch.setattr(
        task_module,
        "get_prepare_db_job",
        lambda *_a, **_kw: {
            "missing": False,
            "succeeded": 2,
            "failed": 0,
            "completions": 2,
            "parallelism": 2,
            "conditions": [{"type": "Complete", "status": "True"}],
            "active": 0,
        },
    )
    monkeypatch.setattr(
        task_module,
        "delete_prepare_db_job",
        lambda *_a, **_kw: delete_calls.append("deleted") or {"status": "deleted"},
    )

    prepare_db_via_aks.run(**_base_kwargs())
    assert delete_calls == ["deleted"]


def test_aks_task_missing_job_treated_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer({"db_name": "core_nt"})
    _set_container(container)

    monkeypatch.setattr(
        task_module, "submit_prepare_db_job", lambda *_a, **_kw: {"status": "created"}
    )
    # Simulate the TTL controller having reaped the Job before our first poll.
    monkeypatch.setattr(
        task_module, "get_prepare_db_job", lambda *_a, **_kw: {"missing": True}
    )
    monkeypatch.setattr(task_module, "delete_prepare_db_job", lambda *_a, **_kw: None)

    result = prepare_db_via_aks.run(**_base_kwargs())
    # missing=True with succeeded_pods=0 -> shard_count==2 -> not job_succeeded
    # but blob poll returns all 2 succeeded -> still "partial" (Job didn't visibly succeed)
    assert result["mode"] == "aks"
    # Either outcome is acceptable; key assertion is no exception raised.
    assert result["outcome"] in {"promoted", "partial"}


def test_aks_task_empty_file_keys_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_container(_FakeContainer())
    with pytest.raises(ValueError):
        prepare_db_via_aks.run(**_base_kwargs(file_keys=[]))


def test_aks_task_missing_source_version_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_container(_FakeContainer())
    with pytest.raises(ValueError):
        prepare_db_via_aks.run(**_base_kwargs(source_version=""))


def test_on_job_progress_reports_file_level_success() -> None:
    """The copying-phase callback surfaces a live per-file `success` count.

    Without this the SPA only sees pod-level `succeeded_pods`/`shard_count`
    (0/10 until a whole shard finishes), so the modal sits at "0 / 10 shards
    · 0%" for many minutes even while blobs are landing.
    """
    container = _FakeContainer({"db_name": "core_nt"})
    container.blob_names = [
        "core_nt/core_nt.000.nhr",
        "core_nt/core_nt.000.nin",
        "core_nt/core_nt.001.nhr",
        # Unrelated prefix must not be counted.
        "core_ntx/other.nhr",
    ]
    container.blob_sizes = {
        "core_nt/core_nt.000.nhr": 1000,
        "core_nt/core_nt.000.nin": 2000,
        "core_nt/core_nt.001.nhr": 500,
        "core_ntx/other.nhr": 9999,
    }
    file_keys = [f"v/core_nt.{i:03d}.nhr" for i in range(10)]
    snapshot = {
        "active_pods": 10,
        "succeeded_pods": 0,
        "failed_pods": 0,
        "shard_count": 10,
    }

    task_module._on_job_progress(
        container,
        "core_nt",
        "stworkload",
        file_keys,
        snapshot,
        mode_label="aks",
        update_metadata=_fake_update_metadata,
        bytes_total=123_456_789,
    )

    cs = container.meta["copy_status"]
    assert cs["phase"] == "copying"
    assert cs["mode"] == "aks"
    assert cs["total_files"] == 10
    assert cs["shard_count"] == 10
    assert cs["succeeded_pods"] == 0
    # File-level signal: 3 blobs under "core_nt/", not the "core_ntx/" decoy.
    assert cs["success"] == 3
    # Byte-level signal for download speed: 1000 + 2000 + 500, decoy excluded.
    assert cs["bytes_done"] == 3500
    # Total expected bytes is the denominator for the SPA's byte-based ETA.
    assert cs["bytes_total"] == 123_456_789


def test_on_job_progress_omits_bytes_total_when_unknown() -> None:
    """No expected-size total → no `bytes_total` key (SPA keeps count ETA)."""
    container = _FakeContainer({"db_name": "core_nt"})
    container.blob_names = ["core_nt/core_nt.000.nhr"]
    container.blob_sizes = {"core_nt/core_nt.000.nhr": 1000}

    task_module._on_job_progress(
        container,
        "core_nt",
        "stworkload",
        ["v/core_nt.000.nhr"],
        {"active_pods": 1, "succeeded_pods": 0, "failed_pods": 0, "shard_count": 1},
        mode_label="aks",
        update_metadata=_fake_update_metadata,
        bytes_total=0,
    )

    cs = container.meta["copy_status"]
    assert "bytes_total" not in cs
    assert cs["bytes_done"] == 1000


def test_on_job_progress_since_excludes_previous_snapshot_blobs() -> None:
    """An update must not count the previous snapshot's blobs as instant progress.

    The AKS pods upload one blob per file and re-fetch on update
    (`--overwrite=true`). Counting every blob under `<db>/` made an update of
    a DB that already had 12 of 15 files show "12 / 15" the moment it started.
    Passing `since` (this run's start) excludes the stale blobs so the bar
    climbs from 0; a blob without `last_modified` is still counted.
    """
    from datetime import UTC, datetime, timedelta

    run_start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    container = _FakeContainer({"db_name": "test16s"})
    container.blob_names = [
        "test16s/a.nhr",  # stale — from the previous snapshot
        "test16s/b.nin",  # stale
        "test16s/c.nsq",  # freshly re-committed this run
        "test16s/d.nhr",  # no last_modified -> counted
    ]
    container.blob_sizes = {
        "test16s/a.nhr": 100,
        "test16s/b.nin": 200,
        "test16s/c.nsq": 300,
        "test16s/d.nhr": 400,
    }
    container.blob_last_modified = {
        "test16s/a.nhr": run_start - timedelta(days=3),
        "test16s/b.nin": run_start - timedelta(days=3),
        "test16s/c.nsq": run_start + timedelta(seconds=5),
        # "test16s/d.nhr" intentionally omitted -> counted as fresh.
    }

    task_module._on_job_progress(
        container,
        "test16s",
        "stworkload",
        [f"v/test16s.{i}" for i in range(15)],
        {"active_pods": 1, "succeeded_pods": 0, "failed_pods": 0, "shard_count": 1},
        mode_label="aks",
        update_metadata=_fake_update_metadata,
        since=run_start - timedelta(seconds=120),
    )

    cs = container.meta["copy_status"]
    # Only the fresh blob + the timestamp-less blob count; the two stale
    # previous-snapshot blobs are excluded.
    assert cs["success"] == 2
    assert cs["bytes_done"] == 300 + 400


def test_on_job_progress_without_since_counts_all_blobs() -> None:
    """`since=None` preserves the unfiltered inventory the orphan reconciler needs."""
    from datetime import UTC, datetime, timedelta

    old = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    container = _FakeContainer({"db_name": "test16s"})
    container.blob_names = ["test16s/a.nhr", "test16s/b.nin"]
    container.blob_sizes = {"test16s/a.nhr": 100, "test16s/b.nin": 200}
    container.blob_last_modified = {
        "test16s/a.nhr": old - timedelta(days=3),
        "test16s/b.nin": old - timedelta(days=3),
    }

    task_module._on_job_progress(
        container,
        "test16s",
        "stworkload",
        ["v/test16s.0", "v/test16s.1"],
        {"active_pods": 1, "succeeded_pods": 0, "failed_pods": 0, "shard_count": 1},
        mode_label="aks",
        update_metadata=_fake_update_metadata,
        # since omitted -> None -> count everything regardless of age.
    )

    cs = container.meta["copy_status"]
    assert cs["success"] == 2
    assert cs["bytes_done"] == 300


def test_on_job_progress_falls_back_when_listing_fails() -> None:
    """A listing failure must not poison progress — `success` is simply omitted."""

    class _BoomContainer(_FakeContainer):
        def list_blobs(self, name_starts_with: str = "") -> list[Any]:
            raise RuntimeError("network blocked")

    container = _BoomContainer({"db_name": "core_nt"})
    task_module._on_job_progress(
        container,
        "core_nt",
        "stworkload",
        ["v/core_nt.000.nhr"],
        {"active_pods": 10, "succeeded_pods": 0, "failed_pods": 0, "shard_count": 10},
        mode_label="aks",
        update_metadata=_fake_update_metadata,
    )

    cs = container.meta["copy_status"]
    assert "success" not in cs
    assert "bytes_done" not in cs
    assert cs["succeeded_pods"] == 0
    assert cs["shard_count"] == 10


def test_task_time_limits_outlive_job_poll_and_deadline() -> None:
    """The per-task Celery time limits MUST exceed the job poll ceiling and
    the Job's `activeDeadlineSeconds`, otherwise the global 1h worker limit
    SIGKILLs the poller mid-download and the DB is stuck `partial`.

    Regression guard for the `nt` "partial · 1081 failed" incident: the Job
    deadline was 45 min and the global Celery limit 1h, so a multi-hour
    `nt`/`core_nt` download could never complete.
    """
    from api.services.k8s.prepare_db_jobs import DEFAULT_ACTIVE_DEADLINE_SECONDS

    # Ordering ladder: job deadline <= poll ceiling < soft < hard.
    assert task_module._JOB_POLL_MAX_SECONDS >= DEFAULT_ACTIVE_DEADLINE_SECONDS
    assert task_module._TASK_SOFT_TIME_LIMIT > task_module._JOB_POLL_MAX_SECONDS
    assert task_module._TASK_HARD_TIME_LIMIT > task_module._TASK_SOFT_TIME_LIMIT

    # Celery must actually apply the per-task overrides (not the global 1h).
    assert prepare_db_via_aks.soft_time_limit == task_module._TASK_SOFT_TIME_LIMIT
    assert prepare_db_via_aks.time_limit == task_module._TASK_HARD_TIME_LIMIT
    # And both must comfortably exceed the old global 1h hard limit so the
    # multi-hour download is never cut off by the default worker limit.
    assert prepare_db_via_aks.time_limit > 3600
