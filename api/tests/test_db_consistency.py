"""Tests for DB volume/shard consistency reconciliation.

Responsibility: Verify the ghost-volume detection + prune + re-shard heal and its
    safety guards — njs authority required to prune, defensive abort when ghosts
    exceed half the volumes, no-authority skip, and the reconcile status machine
    (healed / clean / reshard_only / aborted / skipped).
Edit boundaries: Unit tests only; Storage SDK is faked and the sharding
    primitives are monkeypatched. No live Azure.
Key entry points: the ``test_*`` functions.
Risky contracts: the 50% ghost-fraction cap and the "no njs authority -> never
    prune" guard are the load-bearing safety properties — keep their tests.
Validation: ``uv run pytest -q api/tests/test_db_consistency.py``.
"""

from __future__ import annotations

import json

from api.services.db import consistency


class _FakeBlob:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContainer:
    def __init__(self, names: list[str]) -> None:
        self._names = list(names)
        self.deleted: list[str] = []

    def list_blobs(self, name_starts_with: str = ""):
        return [_FakeBlob(n) for n in list(self._names) if n.startswith(name_starts_with)]

    def delete_blob(self, name: str) -> None:
        self.deleted.append(name)
        if name in self._names:
            self._names.remove(name)

    def get_blob_client(self, name: str):
        return name


class _FakeSvc:
    def __init__(self, cc: _FakeContainer) -> None:
        self._cc = cc

    def get_container_client(self, _container: str) -> _FakeContainer:
        return self._cc


def _volumes(n: int) -> list[str]:
    return [f"core_nt.{i:02d}" for i in range(n)]


# --------------------------------------------------------------------------- #
# authority + ghost detection
# --------------------------------------------------------------------------- #
def test_read_authoritative_volume_count(monkeypatch) -> None:
    raw = json.dumps({"number-of-volumes": 79}).encode()
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda *a, **k: _FakeSvc(_FakeContainer([])),
    )
    monkeypatch.setattr("api.services.storage.data.read_metadata_blob_bytes", lambda bc, **k: raw)
    assert consistency.read_authoritative_volume_count(None, "acct", "core_nt") == 79


def test_read_authoritative_volume_count_missing_returns_none(monkeypatch) -> None:
    def _boom(bc, **k):
        raise RuntimeError("no njs")

    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda *a, **k: _FakeSvc(_FakeContainer([])),
    )
    monkeypatch.setattr("api.services.storage.data.read_metadata_blob_bytes", _boom)
    assert consistency.read_authoritative_volume_count(None, "acct", "core_nt") is None


def test_find_ghost_volumes(monkeypatch) -> None:
    monkeypatch.setattr(consistency, "read_authoritative_volume_count", lambda *a, **k: 79)
    monkeypatch.setattr(consistency, "list_db_volumes", lambda *a, **k: (_volumes(94), 0))
    count, ghosts, actual = consistency.find_ghost_volumes(None, "acct", "core_nt")
    assert count == 79
    assert actual == 94
    assert len(ghosts) == 15  # 79..93
    assert "core_nt.79" in ghosts
    assert "core_nt.78" not in ghosts


# --------------------------------------------------------------------------- #
# prune guards
# --------------------------------------------------------------------------- #
def test_prune_skips_when_no_authority(monkeypatch) -> None:
    monkeypatch.setattr(consistency, "read_authoritative_volume_count", lambda *a, **k: None)
    monkeypatch.setattr(consistency, "list_db_volumes", lambda *a, **k: (_volumes(94), 0))
    result = consistency.prune_ghost_volumes(None, "acct", "core_nt")
    assert result["status"] == "skipped"
    assert result["pruned"] == 0


def test_prune_aborts_when_too_many_ghosts(monkeypatch) -> None:
    # njs claims 10 but storage has 94 -> 84 ghosts = 89% > 50% cap -> abort.
    monkeypatch.setattr(consistency, "read_authoritative_volume_count", lambda *a, **k: 10)
    monkeypatch.setattr(consistency, "list_db_volumes", lambda *a, **k: (_volumes(94), 0))
    result = consistency.prune_ghost_volumes(None, "acct", "core_nt")
    assert result["status"] == "aborted"
    assert result["reason"] == "too_many_ghosts"
    assert result["pruned"] == 0


def test_prune_clean_when_no_ghosts(monkeypatch) -> None:
    monkeypatch.setattr(consistency, "read_authoritative_volume_count", lambda *a, **k: 79)
    monkeypatch.setattr(consistency, "list_db_volumes", lambda *a, **k: (_volumes(79), 0))
    result = consistency.prune_ghost_volumes(None, "acct", "core_nt")
    assert result["status"] == "clean"
    assert result["pruned"] == 0


def test_prune_deletes_only_ghost_blobs(monkeypatch) -> None:
    names = [f"core_nt/core_nt.{i:02d}.nsq" for i in range(94)] + [
        f"core_nt/core_nt.{i:02d}.nin" for i in range(94)
    ]
    cc = _FakeContainer(names)
    monkeypatch.setattr(consistency, "read_authoritative_volume_count", lambda *a, **k: 79)
    monkeypatch.setattr(consistency, "list_db_volumes", lambda *a, **k: (_volumes(94), 0))
    monkeypatch.setattr("api.services.storage.data._blob_service", lambda *a, **k: _FakeSvc(cc))
    result = consistency.prune_ghost_volumes(None, "acct", "core_nt")
    assert result["status"] == "pruned"
    # 15 ghost volumes (79..93) x 2 files each = 30 deletes.
    assert result["pruned"] == 30
    assert all(int(d.rsplit("/", 1)[-1].split(".")[1]) >= 79 for d in cc.deleted)
    # 00..78 stay.
    assert "core_nt/core_nt.78.nsq" not in cc.deleted
    assert "core_nt/core_nt.00.nsq" not in cc.deleted


# --------------------------------------------------------------------------- #
# reconcile status machine
# --------------------------------------------------------------------------- #
def test_reconcile_heals_after_prune(monkeypatch) -> None:
    monkeypatch.setattr(
        consistency,
        "prune_ghost_volumes",
        lambda *a, **k: {"status": "pruned", "authoritative": 79, "pruned": 30},
    )
    monkeypatch.setattr(consistency, "delete_shard_layouts", lambda *a, **k: 20)
    monkeypatch.setattr(
        consistency,
        "ensure_shard_sets",
        lambda *a, **k: {"total_volumes": 79, "shard_sets": [1, 2, 10], "errors": []},
    )
    result = consistency.reconcile_db_consistency(None, "acct", "core_nt")
    assert result["status"] == "healed"
    assert result["resharded"] is True
    assert result["shard"]["shard_sets"] == [1, 2, 10]


def test_reconcile_clean_no_action(monkeypatch) -> None:
    monkeypatch.setattr(
        consistency,
        "prune_ghost_volumes",
        lambda *a, **k: {"status": "clean", "authoritative": 79, "actual": 79, "pruned": 0},
    )
    monkeypatch.setattr(consistency, "shard_layout_needs_rebuild", lambda *a, **k: False)
    result = consistency.reconcile_db_consistency(None, "acct", "core_nt")
    assert result["status"] == "clean"
    assert result["resharded"] is False


def test_reconcile_reshards_stale_layout_without_ghosts(monkeypatch) -> None:
    # No ghosts pruned, but the shard layout still references out-of-range
    # volumes (a prune succeeded earlier but the reshard failed) -> reshard.
    monkeypatch.setattr(
        consistency,
        "prune_ghost_volumes",
        lambda *a, **k: {"status": "clean", "authoritative": 79, "actual": 79, "pruned": 0},
    )
    monkeypatch.setattr(consistency, "shard_layout_needs_rebuild", lambda *a, **k: True)
    monkeypatch.setattr(consistency, "delete_shard_layouts", lambda *a, **k: 20)
    monkeypatch.setattr(
        consistency,
        "ensure_shard_sets",
        lambda *a, **k: {"total_volumes": 79, "shard_sets": [1], "errors": []},
    )
    result = consistency.reconcile_db_consistency(None, "acct", "core_nt")
    assert result["status"] == "reshard_only"
    assert result["resharded"] is True


def test_reconcile_propagates_abort(monkeypatch) -> None:
    monkeypatch.setattr(
        consistency,
        "prune_ghost_volumes",
        lambda *a, **k: {"status": "aborted", "reason": "too_many_ghosts", "pruned": 0},
    )
    result = consistency.reconcile_db_consistency(None, "acct", "core_nt")
    assert result["status"] == "aborted"
    assert "shard" not in result  # never re-shards on an abort


# --------------------------------------------------------------------------- #
# reconcile-all iteration
# --------------------------------------------------------------------------- #
def test_reconcile_all_iterates_and_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.storage.orphan_prepare_db._resolve_workload_storage_account",
        lambda: "acct",
    )
    monkeypatch.setattr(
        "api.services.storage.orphan_prepare_db._iter_metadata_db_names",
        lambda cc, limit: iter(["core_nt", "nt"]),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda *a, **k: _FakeSvc(_FakeContainer([])),
    )
    calls: list[str] = []

    def _fake_reconcile(cred, acct, db, **k):
        calls.append(db)
        return {"status": "healed" if db == "core_nt" else "clean"}

    monkeypatch.setattr(consistency, "reconcile_db_consistency", _fake_reconcile)
    result = consistency.reconcile_all_db_consistency(None)
    assert result["checked"] == 2
    assert result["healed"] == 1
    assert calls == ["core_nt", "nt"]


def test_reconcile_all_skips_without_storage_account(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.storage.orphan_prepare_db._resolve_workload_storage_account",
        lambda: "",
    )
    result = consistency.reconcile_all_db_consistency(None)
    assert result["status"] == "skipped"
    assert result["checked"] == 0


# --------------------------------------------------------------------------- #
# authority parse guards (unparseable / non-positive -> no authority)
# --------------------------------------------------------------------------- #
def test_read_authoritative_unparseable_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda *a, **k: _FakeSvc(_FakeContainer([])),
    )
    for payload in (b"not-json", b"{}", b'{"number-of-volumes": 0}', b'{"number-of-volumes": -3}'):
        monkeypatch.setattr(
            "api.services.storage.data.read_metadata_blob_bytes",
            lambda bc, _p=payload, **k: _p,
        )
        assert consistency.read_authoritative_volume_count(None, "acct", "core_nt") is None


# --------------------------------------------------------------------------- #
# delete_shard_layouts (real function against the fake container)
# --------------------------------------------------------------------------- #
def test_delete_shard_layouts_removes_preset_aliases(monkeypatch) -> None:
    names = [
        "10shards/core_nt_shard_09/core_nt_shard_09.nal",
        "10shards/core_nt_shard_09/core_nt_shard_09.manifest",
        "5shards/core_nt_shard_00/core_nt_shard_00.nal",
        "core_nt/core_nt.njs",  # must survive
    ]
    cc = _FakeContainer(names)
    monkeypatch.setattr("api.services.storage.data._blob_service", lambda *a, **k: _FakeSvc(cc))
    deleted = consistency.delete_shard_layouts(None, "acct", "core_nt")
    assert deleted == 3
    assert "core_nt/core_nt.njs" in cc._names
    assert not any("shards/core_nt_shard_" in n for n in cc._names)


# --------------------------------------------------------------------------- #
# shard_layout_needs_rebuild (real function; reads the .nal DBLIST)
# --------------------------------------------------------------------------- #
def _patch_nal(monkeypatch, cc: _FakeContainer, content: dict[str, str]) -> None:
    monkeypatch.setattr("api.services.storage.data._blob_service", lambda *a, **k: _FakeSvc(cc))
    monkeypatch.setattr(
        "api.services.storage.data.read_metadata_blob_text",
        lambda bc, **k: content[bc],  # get_blob_client returns the name string
    )


def test_shard_layout_needs_rebuild_true_for_out_of_range(monkeypatch) -> None:
    nal = "10shards/core_nt_shard_09/core_nt_shard_09.nal"
    cc = _FakeContainer([nal])
    _patch_nal(
        monkeypatch,
        cc,
        {nal: "TITLE core_nt_shard_09\nDBLIST /blast/blastdb/core_nt.81\n"},
    )
    assert consistency.shard_layout_needs_rebuild(None, "acct", "core_nt", 79) is True


def test_shard_layout_needs_rebuild_false_when_in_range(monkeypatch) -> None:
    nal = "10shards/core_nt_shard_09/core_nt_shard_09.nal"
    cc = _FakeContainer([nal])
    _patch_nal(
        monkeypatch,
        cc,
        {nal: "TITLE core_nt_shard_09\nDBLIST /blast/blastdb/core_nt.72 core_nt.78\n"},
    )
    assert consistency.shard_layout_needs_rebuild(None, "acct", "core_nt", 79) is False


def test_shard_layout_needs_rebuild_false_when_no_layout(monkeypatch) -> None:
    cc = _FakeContainer([])
    _patch_nal(monkeypatch, cc, {})
    assert consistency.shard_layout_needs_rebuild(None, "acct", "core_nt", 79) is False


# --------------------------------------------------------------------------- #
# reconcile-all: never race a live prepare-db (lock held -> skip that DB)
# --------------------------------------------------------------------------- #
def test_reconcile_all_skips_locked_db(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.services.storage.orphan_prepare_db._resolve_workload_storage_account",
        lambda: "acct",
    )
    monkeypatch.setattr(
        "api.services.storage.orphan_prepare_db._iter_metadata_db_names",
        lambda cc, limit: iter(["core_nt", "busy_db"]),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda *a, **k: _FakeSvc(_FakeContainer([])),
    )

    class _Lock:
        def __init__(self, ok: bool) -> None:
            self._ok = ok

        def acquire(self, blocking: bool = True) -> bool:
            return self._ok

        def release(self) -> None:
            pass

    monkeypatch.setattr(
        "api.services.storage.prepare_db_locks.prepare_db_lock",
        lambda account, db: _Lock(db != "busy_db"),
    )
    seen: list[str] = []

    def _fake_reconcile(cred, acct, db, **k):
        seen.append(db)
        return {"status": "healed", "prune": {"status": "pruned"}}

    monkeypatch.setattr(consistency, "reconcile_db_consistency", _fake_reconcile)
    result = consistency.reconcile_all_db_consistency(None)
    assert result["checked"] == 2
    assert result["healed"] == 1
    assert seen == ["core_nt"]  # busy_db is locked by a live prepare-db -> skipped


# --------------------------------------------------------------------------- #
# beat task gate (charter §12a Rule 4 — default OFF, explicit opt-in)
# --------------------------------------------------------------------------- #
def test_beat_task_disabled_by_default(monkeypatch) -> None:
    from api.tasks.storage.reconcile_db_consistency import (
        reconcile_db_consistency as beat_task,
    )

    monkeypatch.delenv("DB_CONSISTENCY_RECONCILE_ENABLED", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(
        "api.services.db.consistency.reconcile_all_db_consistency",
        lambda *a, **k: called.update(n=called["n"] + 1),
    )
    result = beat_task.run(limit=10)
    assert result == {"status": "disabled"}
    assert called["n"] == 0


def test_beat_task_runs_when_enabled(monkeypatch) -> None:
    from api.tasks.storage.reconcile_db_consistency import (
        reconcile_db_consistency as beat_task,
    )

    monkeypatch.setenv("DB_CONSISTENCY_RECONCILE_ENABLED", "true")
    monkeypatch.setattr("api.tasks.storage.get_credential", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(
        "api.services.db.consistency.reconcile_all_db_consistency",
        lambda *a, **k: called.update(n=called["n"] + 1) or {"status": "ok", "healed": 0},
    )
    result = beat_task.run(limit=10)
    assert result["status"] == "ok"
    assert called["n"] == 1
