"""Unit tests for `plan_prepare_db_shards` — issue #7 Phase 1 LPT planner.

Responsibility: Lock the LPT bin-pack contract so a regression that
    silently switches back to round-robin (or off-by-one shard counts)
    is caught before the AKS Job ever schedules.
Edit boundaries: Pure unit tests over `api.services.k8s.prepare_db_jobs`.
    No K8s session, no Storage, no Celery — they belong in
    `test_prepare_db_aks_route.py` / `test_prepare_db_aks_task.py`.
Key entry points: `test_lpt_isolates_large_file`,
    `test_deterministic_ordering`, `test_shard_count_capped_by_max_pods`.
Risky contracts: The default `max_pods=10` / `files_per_pod=50` numbers
    are mirrored by the route's env-var defaults; updating one without
    the other will surface here.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_planner.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s.prepare_db_jobs import plan_prepare_db_shards


def test_empty_input_returns_empty_list() -> None:
    assert plan_prepare_db_shards([]) == []


def test_single_file_single_shard() -> None:
    shards = plan_prepare_db_shards(["a.nhr"], max_pods=10)
    assert shards == [["a.nhr"]]


def test_max_pods_one_collapses_to_single_shard() -> None:
    files = [f"f{i:03d}.nhr" for i in range(8)]
    shards = plan_prepare_db_shards(files, max_pods=1, files_per_pod=50)
    assert len(shards) == 1
    assert sorted(shards[0]) == sorted(files)


def test_shard_count_capped_by_max_pods() -> None:
    # 300 files / files_per_pod=50 = 6 shards, but max_pods=4 caps it.
    files = [f"f{i:03d}.nhr" for i in range(300)]
    shards = plan_prepare_db_shards(files, max_pods=4, files_per_pod=50)
    assert len(shards) == 4
    # All files placed, none duplicated.
    flat = [k for shard in shards for k in shard]
    assert sorted(flat) == sorted(files)


def test_shard_count_uses_files_per_pod_when_below_max_pods() -> None:
    files = [f"f{i:03d}.nhr" for i in range(8)]
    shards = plan_prepare_db_shards(files, max_pods=10, files_per_pod=3)
    # ceil(8 / 3) == 3 — never spawn more shards than necessary
    assert len(shards) == 3


def test_lpt_isolates_large_file() -> None:
    # One 10 GB file should land on its own shard when the smaller files
    # together weigh less than it. Round-robin would scatter the smalls.
    files = ["big.nsq", "s1.nhr", "s2.nhr", "s3.nhr", "s4.nhr"]
    sizes = {
        "big.nsq": 10 * 1024**3,
        "s1.nhr": 1024,
        "s2.nhr": 1024,
        "s3.nhr": 1024,
        "s4.nhr": 1024,
    }
    shards = plan_prepare_db_shards(
        files, sizes=sizes, max_pods=3, files_per_pod=2
    )
    # Shard with the big file should be exactly [big.nsq].
    big_shards = [s for s in shards if "big.nsq" in s]
    assert len(big_shards) == 1
    assert big_shards[0] == ["big.nsq"]


def test_lpt_balances_by_total_bytes() -> None:
    # 4 large + 4 small files split over 2 shards: each shard should get
    # 2 large + 2 small (balanced bytes), not 4 large + 4 small.
    files = ["L1", "L2", "L3", "L4", "s1", "s2", "s3", "s4"]
    sizes = {
        "L1": 1_000_000,
        "L2": 1_000_000,
        "L3": 1_000_000,
        "L4": 1_000_000,
        "s1": 1,
        "s2": 1,
        "s3": 1,
        "s4": 1,
    }
    shards = plan_prepare_db_shards(
        files, sizes=sizes, max_pods=2, files_per_pod=4
    )
    assert len(shards) == 2
    large_per_shard = [sum(1 for k in shard if k.startswith("L")) for shard in shards]
    assert sorted(large_per_shard) == [2, 2]


def test_unknown_sizes_fall_back_to_count_balance() -> None:
    files = [f"f{i:03d}.nhr" for i in range(6)]
    shards = plan_prepare_db_shards(files, max_pods=3, files_per_pod=2)
    # No sizes supplied -> each file weighted equally -> 6 / 3 = 2 each
    assert len(shards) == 3
    assert [len(s) for s in shards] == [2, 2, 2]


def test_deterministic_ordering_same_input_same_output() -> None:
    files = ["alpha", "bravo", "charlie", "delta", "echo"]
    sizes = {"alpha": 100, "bravo": 200, "charlie": 100, "delta": 300, "echo": 200}
    a = plan_prepare_db_shards(files, sizes=sizes, max_pods=3, files_per_pod=2)
    b = plan_prepare_db_shards(files, sizes=sizes, max_pods=3, files_per_pod=2)
    assert a == b


def test_invalid_files_per_pod_raises() -> None:
    with pytest.raises(ValueError):
        plan_prepare_db_shards(["a"], files_per_pod=0)


def test_invalid_max_pods_raises() -> None:
    with pytest.raises(ValueError):
        plan_prepare_db_shards(["a"], max_pods=0)


def test_all_files_assigned_no_duplicates() -> None:
    files = [f"f{i:04d}.nhr" for i in range(123)]
    sizes = {f: (i % 7 + 1) * 1024 for i, f in enumerate(files)}
    shards = plan_prepare_db_shards(
        files, sizes=sizes, max_pods=10, files_per_pod=50
    )
    flat = [k for shard in shards for k in shard]
    assert sorted(flat) == sorted(files)
    assert len(set(flat)) == len(files)
