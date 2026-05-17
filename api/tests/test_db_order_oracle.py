from __future__ import annotations

import pytest
from api.services.db_order_oracle import (
    build_db_order_oracle_job_plan,
    oracle_part_blob_path,
    oracle_part_url,
    oracle_status_blob_path,
)


def test_oracle_blob_paths_are_stable() -> None:
    assert oracle_status_blob_path("core_nt") == "metadata/oracles/core_nt/status.json"
    assert (
        oracle_part_blob_path("core_nt", "20260518120000-abcd1234", "09")
        == "metadata/oracles/core_nt/parts/20260518120000-abcd1234/09.txt"
    )
    assert oracle_part_url("elbstg01", "core_nt", "20260518120000-abcd1234", "09") == (
        "https://elbstg01.blob.core.windows.net/blast-db/"
        "metadata/oracles/core_nt/parts/20260518120000-abcd1234/09.txt"
    )


def test_build_db_order_oracle_job_plan_pins_shards_to_nodes() -> None:
    plan = build_db_order_oracle_job_plan(
        db_name="core_nt",
        storage_account="elbstg01",
        run_id="20260518120000-abcd1234",
        shard_nodes=[("00", "aks-blastpool-000001"), ("01", "aks-blastpool-000002")],
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )

    assert len(plan.jobs) == 2
    assert len(plan.part_urls) == 2
    first = plan.jobs[0]
    pod_spec = first["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}

    assert first["metadata"]["name"] == "oracle-core-nt-00-20260518120000-abcd1234"
    assert first["metadata"]["labels"]["app"] == "elb-db-order-oracle"
    assert pod_spec["nodeName"] == "aks-blastpool-000001"
    assert env["ELB_DB"] == "core_nt_shard_00"
    assert env["ELB_ORACLE_PART_URL"].endswith(
        "/metadata/oracles/core_nt/parts/20260518120000-abcd1234/00.txt"
    )
    assert pod_spec["volumes"][0]["hostPath"]["path"] == "/workspace/blast"
    assert "blastdbcmd -db" in container["args"][0]
    assert "azcopy cp" in container["args"][0]


def test_build_db_order_oracle_job_plan_uses_per_shard_host_paths() -> None:
    plan = build_db_order_oracle_job_plan(
        db_name="core_nt",
        storage_account="elbstg01",
        run_id="20260518120000-abcd1234",
        shard_nodes=[
            ("00", "aks-blastpool-000001", "/workspace/blastdb/core_nt/00"),
            ("01", "aks-blastpool-000002", "/workspace/blastdb/core_nt/01"),
        ],
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )

    assert plan.jobs[0]["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"] == (
        "/workspace/blastdb/core_nt/00"
    )
    assert plan.jobs[1]["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"] == (
        "/workspace/blastdb/core_nt/01"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_name", "../core_nt"),
        ("storage_account", "ELBSTG01"),
        ("run_id", "bad/id"),
        ("image", "bad image"),
    ],
)
def test_build_db_order_oracle_job_plan_rejects_unsafe_inputs(field: str, value: str) -> None:
    kwargs = {
        "db_name": "core_nt",
        "storage_account": "elbstg01",
        "run_id": "20260518120000-abcd1234",
        "shard_nodes": [("00", "aks-blastpool-000001")],
        "image": "elbacr01.azurecr.io/ncbi/elb:1.4.0",
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        build_db_order_oracle_job_plan(**kwargs)
