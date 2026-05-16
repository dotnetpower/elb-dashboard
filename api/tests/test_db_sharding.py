"""Unit tests for api.services.db_sharding.

These tests cover the pure-Python parts (validation, layout planning,
text rendering, partition selection) without touching Azure. Blob-touching
helpers (``list_db_volumes``, ``upload_shard_set``) are exercised via a
``_blob_service`` monkeypatch with a fake container client.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from api.services import db_sharding as dbs


# ---------------------------------------------------------------------------
# Fake blob storage
# ---------------------------------------------------------------------------
@dataclass
class _FakeBlob:
    name: str
    size: int


class _FakeBlobClient:
    def __init__(self, name: str, store: dict[str, bytes]):
        self.name = name
        self._store = store

    def get_blob_properties(self):
        if self.name not in self._store:
            from azure.core.exceptions import ResourceNotFoundError
            raise ResourceNotFoundError(self.name)
        return MagicMock(name=self.name)

    def download_blob(self):
        if self.name not in self._store:
            from azure.core.exceptions import ResourceNotFoundError
            raise ResourceNotFoundError(self.name)
        data = self._store[self.name]
        m = MagicMock()
        m.readall.return_value = data
        return m

    def upload_blob(self, data: bytes, *, overwrite: bool = False) -> None:
        if not overwrite and self.name in self._store:
            raise RuntimeError("blob exists and overwrite=False")
        self._store[self.name] = data


class _FakeContainerClient:
    def __init__(self, blobs: list[_FakeBlob], store: dict[str, bytes]):
        self._blobs = blobs
        self._store = store
        for b in blobs:
            self._store.setdefault(b.name, b"x" * (b.size if b.size > 0 else 0))

    def list_blobs(self, *, name_starts_with: str = "") -> Iterator[_FakeBlob]:
        for b in self._blobs:
            if b.name.startswith(name_starts_with):
                yield b

    def get_blob_client(self, blob_name: str) -> _FakeBlobClient:
        return _FakeBlobClient(blob_name, self._store)


class _FakeBlobService:
    def __init__(self, container: _FakeContainerClient):
        self._container = container

    def get_container_client(self, name: str) -> _FakeContainerClient:
        return self._container


def _patch_blob_service(monkeypatch, container: _FakeContainerClient) -> None:
    fake_svc = _FakeBlobService(container)
    monkeypatch.setattr(dbs, "_blob_service", lambda *a, **k: fake_svc)


# ---------------------------------------------------------------------------
# _validate_db_name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["core_nt", "16S_ribosomal_RNA", "nr", "swissprot", "ref_viruses-2025.04"],
)
def test_validate_db_name_accepts_real_db_names(name: str) -> None:
    dbs._validate_db_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../etc/passwd",
        "core_nt/",
        "core nt",
        "core_nt;rm",
        "../core_nt",
        ".hidden",
        "a" * 65,
    ],
)
def test_validate_db_name_rejects_hostile_input(name: str) -> None:
    with pytest.raises(ValueError):
        dbs._validate_db_name(name)


# ---------------------------------------------------------------------------
# plan_shard_layout
# ---------------------------------------------------------------------------
def test_plan_shard_layout_contiguous_blocks() -> None:
    vols = [f"core_nt.{i:02d}" for i in range(83)]
    layout = dbs.plan_shard_layout("core_nt", vols, 10)
    # 83 / 10 = 9 with remainder 3 -> ceil-divide block=9 -> first 9 shards
    # have 9 vols each (0..8, 9..17, ... 72..80 = 9 shards x 9 = 81), last
    # shard absorbs remaining 2 (81, 82).
    assert layout.num_shards == 10
    assert sum(len(s) for s in layout.shards) == 83
    assert layout.shards[0] == tuple(f"core_nt.{i:02d}" for i in range(9))
    assert layout.shards[8] == tuple(f"core_nt.{i:02d}" for i in range(72, 81))
    assert layout.shards[9] == tuple(f"core_nt.{i:02d}" for i in range(81, 83))


def test_plan_shard_layout_rejects_more_shards_than_volumes() -> None:
    vols = ["x.00", "x.01"]
    with pytest.raises(ValueError, match="exceeds volume count"):
        dbs.plan_shard_layout("x", vols, 5)


def test_plan_shard_layout_single_shard_is_full_db() -> None:
    vols = [f"x.{i:02d}" for i in range(5)]
    layout = dbs.plan_shard_layout("x", vols, 1)
    assert len(layout.shards) == 1
    assert layout.shards[0] == tuple(vols)


def test_plan_shard_layout_rejects_empty_volumes() -> None:
    with pytest.raises(ValueError):
        dbs.plan_shard_layout("x", [], 2)


# ---------------------------------------------------------------------------
# render_manifest / render_nal
# ---------------------------------------------------------------------------
def test_render_manifest_one_volume_per_line_with_trailing_newline() -> None:
    text = dbs.render_manifest(["core_nt.00", "core_nt.01", "core_nt.02"])
    assert text == "core_nt.00\ncore_nt.01\ncore_nt.02\n"


def test_render_manifest_rejects_empty() -> None:
    with pytest.raises(ValueError):
        dbs.render_manifest([])


def test_render_nal_matches_sibling_init_script_format() -> None:
    # Expected format per sibling init-db-shard-aks.sh §
    #   TITLE core_nt_shard_03
    #   DBLIST /blast/blastdb/core_nt.27 /blast/blastdb/core_nt.28 ...
    text = dbs.render_nal(
        db_name="core_nt",
        shard_idx=3,
        num_shards=10,
        volumes=["core_nt.27", "core_nt.28", "core_nt.29"],
    )
    assert text == (
        "TITLE core_nt_shard_03\n"
        "DBLIST /blast/blastdb/core_nt.27 /blast/blastdb/core_nt.28 "
        "/blast/blastdb/core_nt.29\n"
    )


def test_render_nal_rejects_out_of_range_shard_idx() -> None:
    with pytest.raises(ValueError):
        dbs.render_nal("x", shard_idx=5, num_shards=3, volumes=["x.00"])


# ---------------------------------------------------------------------------
# select_partitions_for_submit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "db_gib, num_nodes, machine_type, expected",
    [
        # core_nt 269 GB on E16s_v5 (128 GB RAM) — memory floor = 5.
        # num_nodes=1 → take the memory floor → 5.
        (269, 1, "Standard_E16s_v5", 5),
        # num_nodes=8 dominates memory floor (5) → 8.
        (269, 8, "Standard_E16s_v5", 8),
        # num_nodes=10 dominates → 10.
        (269, 10, "Standard_E16s_v5", 10),
        # E32s_v5 (256 GB RAM) memory floor = 3 → num_nodes wins.
        (269, 5, "Standard_E32s_v5", 5),
        # E64s_v5 (512 GB) memory floor = 2 → num_nodes wins.
        (269, 3, "Standard_E64s_v5", 3),
        # Tiny DB (1 GB) on E16 — memory floor = 1 → num_nodes wins.
        (1, 4, "Standard_E16s_v5", 4),
        # Single node, tiny DB → 1 (default preset).
        (1, 1, "Standard_E16s_v5", 1),
    ],
)
def test_select_partitions_for_submit_matches_v3_design(
    db_gib: int, num_nodes: int, machine_type: str, expected: int
) -> None:
    chosen = dbs.select_partitions_for_submit(
        db_total_bytes=db_gib * 1024**3,
        num_nodes=num_nodes,
        machine_type=machine_type,
    )
    assert chosen == expected


def test_select_partitions_unknown_machine_falls_back_to_64gib() -> None:
    # Unknown machine → assume 64 GiB → memory floor for 269 GB = ceil(269 / 32) = 9
    # num_nodes=1 → take memory floor → snap to next preset (10).
    chosen = dbs.select_partitions_for_submit(
        db_total_bytes=269 * 1024**3,
        num_nodes=1,
        machine_type="Standard_NotARealSku",
    )
    assert chosen == 10


def test_select_partitions_caps_at_largest_preset() -> None:
    # 1000 GB DB on tiny E16 with 1 node → memory floor would be ~16,
    # which is the last preset.
    chosen = dbs.select_partitions_for_submit(
        db_total_bytes=1000 * 1024**3,
        num_nodes=1,
        machine_type="Standard_E16s_v5",
    )
    assert chosen == 10  # last preset in PRESET_SHARD_SETS


def test_select_partitions_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        dbs.select_partitions_for_submit(0, 0, "Standard_E16s_v5")
    with pytest.raises(ValueError):
        dbs.select_partitions_for_submit(-1, 1, "Standard_E16s_v5")


# ---------------------------------------------------------------------------
# partition_prefix_for
# ---------------------------------------------------------------------------
def test_partition_prefix_matches_v3_layout_convention() -> None:
    p = dbs.partition_prefix_for("elbstg01", "core_nt", 10)
    assert p == (
        "https://elbstg01.blob.core.windows.net/blast-db/"
        "10shards/core_nt_shard_"
    )


# ---------------------------------------------------------------------------
# list_db_volumes (with fake blob storage)
# ---------------------------------------------------------------------------
def test_list_db_volumes_multi_volume_core_nt(monkeypatch: pytest.MonkeyPatch) -> None:
    blobs = []
    # Fake 5-volume core_nt with marker (.nsq) + auxiliary (.nhr, .nin).
    for i in range(5):
        blobs.append(_FakeBlob(f"core_nt/core_nt.{i:02d}.nsq", 1_000_000_000))
        blobs.append(_FakeBlob(f"core_nt/core_nt.{i:02d}.nhr", 50_000))
        blobs.append(_FakeBlob(f"core_nt/core_nt.{i:02d}.nin", 50_000))
    # Auxiliary DB-level files (no .NN suffix) should be ignored as volumes.
    blobs.append(_FakeBlob("core_nt/core_nt.ndb", 1_000))
    blobs.append(_FakeBlob("core_nt/core_nt.nal", 500))  # alias must NOT be a volume
    container = _FakeContainerClient(blobs, store={})
    _patch_blob_service(monkeypatch, container)

    volumes, total = dbs.list_db_volumes(MagicMock(), "elbstg01", "core_nt")
    assert volumes == [f"core_nt.{i:02d}" for i in range(5)]
    # Each volume ~1 GB (.nsq) + 100 KB (.nhr+.nin); we sum *attributable*
    # bytes only (the unattributed .ndb / .nal don't roll up to a volume).
    assert total == 5 * (1_000_000_000 + 100_000)


def test_list_db_volumes_single_volume_db(monkeypatch: pytest.MonkeyPatch) -> None:
    # 16S has just a few files, no .NN suffix.
    blobs = [
        _FakeBlob("16S_ribosomal_RNA/16S_ribosomal_RNA.nsq", 50_000_000),
        _FakeBlob("16S_ribosomal_RNA/16S_ribosomal_RNA.nhr", 1_000),
        _FakeBlob("16S_ribosomal_RNA/16S_ribosomal_RNA.nin", 500),
    ]
    container = _FakeContainerClient(blobs, store={})
    _patch_blob_service(monkeypatch, container)

    volumes, total = dbs.list_db_volumes(MagicMock(), "elbstg01", "16S_ribosomal_RNA")
    assert volumes == ["16S_ribosomal_RNA"]
    assert total == 50_001_500


def test_list_db_volumes_raises_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainerClient([], store={})
    _patch_blob_service(monkeypatch, container)
    with pytest.raises(LookupError):
        dbs.list_db_volumes(MagicMock(), "elbstg01", "missing_db")


def test_read_blastdb_stats_from_njs(monkeypatch: pytest.MonkeyPatch) -> None:
    stats = {
        "number-of-letters": 29_999_612,
        "number-of-sequences": 50_000,
        "bytes-to-cache": 8_150_742,
        "bytes-total": 14_974_393,
    }
    store = {"core_nt/core_nt.njs": json.dumps(stats).encode("utf-8")}
    container = _FakeContainerClient(
        [_FakeBlob("core_nt/core_nt.njs", len(store["core_nt/core_nt.njs"]))],
        store=store,
    )
    _patch_blob_service(monkeypatch, container)

    assert dbs.read_blastdb_stats(MagicMock(), "elbstg01", "core_nt") == {
        "total_letters": 29_999_612,
        "total_sequences": 50_000,
        "bytes_to_cache": 8_150_742,
        "bytes_total": 14_974_393,
    }


# ---------------------------------------------------------------------------
# upload_shard_set + ensure_shard_sets idempotency
# ---------------------------------------------------------------------------
def test_upload_shard_set_writes_manifest_and_nal(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainerClient([], store={})
    _patch_blob_service(monkeypatch, container)

    vols = [f"core_nt.{i:02d}" for i in range(10)]
    result = dbs.upload_shard_set(MagicMock(), "elbstg01", "core_nt", 5, vols)
    assert result.created == 10  # 5 manifests + 5 nal files
    assert result.skipped == 0
    # Sanity check on a sample blob.
    written = container._store
    assert "5shards/core_nt_shard_00/core_nt_shard_00.manifest" in written
    assert "5shards/core_nt_shard_04/core_nt_shard_04.nal" in written
    # Manifest has correct volumes for shard 0 (vols 0..1).
    manifest_0 = written["5shards/core_nt_shard_00/core_nt_shard_00.manifest"].decode()
    assert manifest_0 == "core_nt.00\ncore_nt.01\n"


def test_upload_shard_set_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainerClient([], store={})
    _patch_blob_service(monkeypatch, container)

    vols = [f"core_nt.{i:02d}" for i in range(10)]
    first = dbs.upload_shard_set(MagicMock(), "elbstg01", "core_nt", 5, vols)
    assert first.created == 10
    # Re-run — should detect every shard is present and skip the entire set.
    second = dbs.upload_shard_set(MagicMock(), "elbstg01", "core_nt", 5, vols)
    assert second.created == 0
    assert second.skipped == 2 * 5


def test_ensure_shard_sets_skips_oversized_presets(monkeypatch: pytest.MonkeyPatch) -> None:
    # 3-volume DB → only N=1, 2, 3 are achievable; N=4..10 should be skipped.
    njs = json.dumps({"number-of-letters": 3_000, "number-of-sequences": 3}).encode()
    blobs = [
        _FakeBlob(f"smalldb/smalldb.{i:02d}.nsq", 1_000) for i in range(3)
    ]
    blobs.append(_FakeBlob("smalldb/smalldb.njs", len(njs)))
    container = _FakeContainerClient(blobs, store={"smalldb/smalldb.njs": njs})
    _patch_blob_service(monkeypatch, container)

    summary = dbs.ensure_shard_sets(MagicMock(), "elbstg01", "smalldb")
    assert summary["total_volumes"] == 3
    assert summary["total_letters"] == 3_000
    assert summary["total_sequences"] == 3
    assert summary["shard_sets"] == [1, 2, 3]
    assert summary["errors"] == []


def test_shard_sets_present_returns_intersection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pre-populate ONLY the N=2 shard set's nal files; N=3 should be missing.
    store: dict[str, bytes] = {
        "2shards/core_nt_shard_00/core_nt_shard_00.nal": b"x",
        "2shards/core_nt_shard_01/core_nt_shard_01.nal": b"x",
    }
    container = _FakeContainerClient([], store=store)
    _patch_blob_service(monkeypatch, container)

    ready = dbs.shard_sets_present(MagicMock(), "elbstg01", "core_nt")
    assert ready == [2]


# ---------------------------------------------------------------------------
# derive_volumes_from_keys
# ---------------------------------------------------------------------------
def test_derive_volumes_from_keys_multi_volume_with_marker_files() -> None:
    keys = []
    for i in range(5):
        # Marker (.nsq) for each volume + auxiliary files.
        keys.append(f"core_nt.{i:02d}.nsq")
        keys.append(f"core_nt.{i:02d}.nhr")
        keys.append(f"core_nt.{i:02d}.nin")
    # Aliases / non-volume DB-level files MUST be ignored.
    keys.append("core_nt.nal")
    keys.append("core_nt.ndb")
    volumes = dbs.derive_volumes_from_keys("core_nt", keys)
    assert volumes == [f"core_nt.{i:02d}" for i in range(5)]


def test_derive_volumes_from_keys_handles_directory_prefix() -> None:
    # NCBI S3 listing returns keys like "v5/2024-01-01-01-05-01/core_nt.00.nsq"
    keys = [
        "v5/2024-01-01-01-05-01/core_nt.00.nsq",
        "v5/2024-01-01-01-05-01/core_nt.01.nsq",
    ]
    volumes = dbs.derive_volumes_from_keys("core_nt", keys)
    assert volumes == ["core_nt.00", "core_nt.01"]


def test_derive_volumes_from_keys_single_volume_db() -> None:
    keys = [
        "16S_ribosomal_RNA.nsq",
        "16S_ribosomal_RNA.nhr",
        "16S_ribosomal_RNA.nin",
    ]
    volumes = dbs.derive_volumes_from_keys("16S_ribosomal_RNA", keys)
    assert volumes == ["16S_ribosomal_RNA"]


def test_derive_volumes_from_keys_empty_when_no_marker() -> None:
    # Only auxiliary files, no .nsq/.psq → no volumes detected.
    keys = ["core_nt.nhr", "core_nt.nin", "core_nt.nal"]
    volumes = dbs.derive_volumes_from_keys("core_nt", keys)
    assert volumes == []


def test_derive_volumes_from_keys_sorts_numerically_not_lexically() -> None:
    keys = [f"db.{i:02d}.nsq" for i in range(12)]
    # Shuffle to confirm sort.
    keys.reverse()
    volumes = dbs.derive_volumes_from_keys("db", keys)
    # Lexical sort would put "db.10" before "db.9" but "db.{i:02d}"
    # zero-pads so numeric and lexical agree here. Use a non-padded form
    # to exercise the numeric path:
    keys_unpadded = [f"db.{i}.nsq" for i in range(12)]
    keys_unpadded.reverse()
    # derive_volumes_from_keys uses _volume_sort_key which is numeric-aware.
    # But our regex requires zero-padded \d+ — actually any digit count
    # matches. Let's verify both return correct order.
    volumes_unpadded = dbs.derive_volumes_from_keys("db", keys_unpadded)
    assert volumes_unpadded == [f"db.{i}" for i in range(12)]
    assert volumes == [f"db.{i:02d}" for i in range(12)]
