"""Tests for DB order-oracle readiness planning.

Responsibility: Verify generation, copy, shard, warmup, and Ready-node gates
    plus deterministic build identity without cloud access.
Edit boundaries: Pure snapshot inputs only; Blob claims and Kubernetes task
    execution belong to their focused suites.
Key entry points: `test_ready_snapshots_build_context`,
    `test_generation_mismatch_is_blocked`,
    `test_incomplete_shard_node_mapping_is_blocked`.
Risky contracts: Every accepted context must represent one homogeneous source
    generation and map every expected shard to one Ready node.
Validation: `uv run pytest -q api/tests/test_oracle_build.py`.
"""

from __future__ import annotations

import pytest
from api.services.db.oracle_build import (
    OracleBuildBlocked,
    plan_oracle_build_from_snapshots,
)


def _db_meta(**overrides: object) -> dict[str, object]:
    return {
        "name": "core_nt",
        "source_version": "v1",
        "copy_status": {"phase": "completed"},
        "sharded": True,
        "shard_sets": [2],
        "shard_layout_schema": 1,
        **overrides,
    }


def _warmup(**overrides: object) -> dict[str, object]:
    database = {
        "name": "core_nt",
        "status": "Ready",
        "source_version": "v1",
        "source_versions": ["v1"],
        "shards": ["00", "01"],
        "pod_statuses": [
            {"shard": "00", "node": "node-a"},
            {"shard": "01", "node": "node-b"},
        ],
        "shard_host_paths": {
            "00": "/workspace/blastdb/core_nt/00",
            "01": "/workspace/blastdb/core_nt/01",
        },
        **overrides,
    }
    return {"databases": [database]}


def test_ready_snapshots_build_context() -> None:
    context = plan_oracle_build_from_snapshots(
        db_name="core_nt",
        db_meta=_db_meta(),
        warmup=_warmup(),
        ready_nodes=["node-a", "node-b"],
        requested_source_version="v1",
    )

    assert context.source_version == "v1"
    assert context.shards == ("00", "01")
    assert context.shard_nodes == (
        ("00", "node-a", "/workspace/blastdb/core_nt/00"),
        ("01", "node-b", "/workspace/blastdb/core_nt/01"),
    )
    assert context.expected_parts == 2
    assert context.identity.startswith("oracle-v1:")


@pytest.mark.parametrize(
    ("db_overrides", "warm_overrides", "code"),
    [
        ({"update_in_progress": True}, {}, "database_updating"),
        ({"copy_status": {"phase": "partial"}}, {}, "database_not_ready"),
        ({"shards_stale": True}, {}, "shards_stale"),
        ({"sharded": False, "shard_sets": []}, {}, "shards_missing"),
        ({}, {"status": "Loading"}, "warmup_not_ready"),
        ({}, {"source_versions": ["v1", "v2"]}, "warmup_generation_mixed"),
        ({}, {"source_version": "v2"}, "warmup_generation_stale"),
    ],
)
def test_readiness_blockers_have_stable_codes(
    db_overrides: dict[str, object],
    warm_overrides: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(OracleBuildBlocked) as caught:
        plan_oracle_build_from_snapshots(
            db_name="core_nt",
            db_meta=_db_meta(**db_overrides),
            warmup=_warmup(**warm_overrides),
            ready_nodes=["node-a", "node-b"],
        )

    assert caught.value.code == code


def test_generation_mismatch_is_blocked() -> None:
    with pytest.raises(OracleBuildBlocked) as caught:
        plan_oracle_build_from_snapshots(
            db_name="core_nt",
            db_meta=_db_meta(),
            warmup=_warmup(),
            ready_nodes=["node-a", "node-b"],
            requested_source_version="v2",
        )

    assert caught.value.code == "source_version_changed"


def test_incomplete_shard_node_mapping_is_blocked() -> None:
    with pytest.raises(OracleBuildBlocked) as caught:
        plan_oracle_build_from_snapshots(
            db_name="core_nt",
            db_meta=_db_meta(),
            warmup=_warmup(pod_statuses=[]),
            ready_nodes=["node-a"],
        )

    assert caught.value.code == "shard_node_mapping_incomplete"
