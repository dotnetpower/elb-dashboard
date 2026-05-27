"""Tests for Warmup Jobs behavior.

Responsibility: Tests for Warmup Jobs behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_nodes`, `test_e16_x10_plan_pins_one_core_nt_shard_per_node`,
`test_plan_tags_warmup_jobs_with_source_version`,
`test_warmup_scripts_configmap_contains_job_scripts`,
`test_plan_rejects_too_few_nodes_for_one_shard_per_node`, `test_plan_rejects_unsafe_inputs`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_warmup_jobs.py`.
"""

from __future__ import annotations

import pytest
from api.services.warmup.jobs import (
    DEFAULT_CONTAINER_DB_PATH,
    attach_pod_progress_to_database_status,
    build_warmup_job_plan,
    build_warmup_scripts_configmap,
    database_status_from_warmup_jobs,
    infer_warmup_pod_phase,
)
from api.tasks.storage import _select_warmup_shard_count


def _nodes(count: int) -> list[str]:
    return [f"aks-blastpool-21457395-vmss{i:06d}" for i in range(count)]


def test_e16_x10_plan_pins_one_core_nt_shard_per_node() -> None:
    plan = build_warmup_job_plan(
        db_name="core_nt",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=10,
        nodes=_nodes(10),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )

    assert plan.num_shards == 10
    assert len(plan.jobs) == 10
    assert plan.nodes == tuple(_nodes(10))

    for idx, job in enumerate(plan.jobs):
        shard = f"{idx:02d}"
        pod_spec = job["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        env = {item["name"]: item["value"] for item in container["env"]}
        host_path = pod_spec["volumes"][0]["hostPath"]["path"]

        assert job["metadata"]["name"] == f"warm-core-nt-{shard}"
        assert job["metadata"]["labels"] == {
            "app": "elb-db-warmup",
            "db": "core_nt",
            "shard": shard,
        }
        assert pod_spec["nodeName"] == _nodes(10)[idx]
        assert env["ELB_DB"] == f"core_nt_shard_{shard}"
        assert env["AZCOPY_CONCURRENCY_VALUE"] == "16"
        assert env["AZCOPY_BUFFER_GB"] == "2"
        assert env["ELB_PARTITION_PREFIX"] == (
            "https://elbstg01.blob.core.windows.net/blast-db/10shards/core_nt_shard_"
        )
        assert host_path == "/workspace/blast"
        assert container["volumeMounts"][0]["mountPath"] == DEFAULT_CONTAINER_DB_PATH
        assert "source /tmp/shard_volpaths.txt" not in container["args"][0]
        assert "CLEANUP partial downloads" in container["args"][0]
        assert "CACHE_INCOMPLETE missing nucleotide volume files" in container["args"][0]
        assert "CACHE_STALE missing source-version marker" in container["args"][0]
        assert "TAXDB_SKIP taxdb files not present in DB prefix" in container["args"][0]
        assert "valid_nsq_count=" in container["args"][0]
        assert "printf '%s' ok > .download-complete" in container["args"][0]
        assert "blast-vmtouch-aks.sh" in container["args"][0]


def test_plan_tags_warmup_jobs_with_source_version() -> None:
    plan = build_warmup_job_plan(
        db_name="core_nt",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=2,
        nodes=_nodes(2),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
        source_version="2026-05-20-00-00-00",
    )

    for job in plan.jobs:
        assert job["metadata"]["annotations"] == {
            "elb.dashboard/source-version": "2026-05-20-00-00-00"
        }
        assert job["spec"]["template"]["metadata"]["annotations"] == {
            "elb.dashboard/source-version": "2026-05-20-00-00-00"
        }
        env = {
            item["name"]: item["value"]
            for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["ELB_DB_SOURCE_VERSION"] == "2026-05-20-00-00-00"


def test_warmup_scripts_configmap_contains_job_scripts() -> None:
    manifest = build_warmup_scripts_configmap()

    assert manifest["metadata"]["name"] == "elb-warmup-scripts"
    assert "init-db-shard-aks.sh" in manifest["data"]
    assert "blast-vmtouch-aks.sh" in manifest["data"]
    assert "azcopy login --identity" in manifest["data"]["init-db-shard-aks.sh"]
    assert (
        "AZCOPY_CONCURRENCY_VALUE=${AZCOPY_CONCURRENCY_VALUE:-16}"
        in manifest["data"]["init-db-shard-aks.sh"]
    )
    assert "taxdb.btd;taxdb.bti" in manifest["data"]["init-db-shard-aks.sh"]
    assert 'cd "${ELB_BLASTDB_DIR:-/blast/blastdb}"' in manifest["data"]["init-db-shard-aks.sh"]
    assert "CLEANUP partial downloads" in manifest["data"]["init-db-shard-aks.sh"]
    assert (
        "CACHE_INCOMPLETE missing nucleotide volume files"
        in manifest["data"]["init-db-shard-aks.sh"]
    )
    assert (
        "TAXDB_SKIP taxdb files not present in DB prefix"
        in manifest["data"]["init-db-shard-aks.sh"]
    )
    assert "CACHE_STALE missing source-version marker" in manifest["data"]["init-db-shard-aks.sh"]
    assert (
        "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}" in manifest["data"]["init-db-shard-aks.sh"]
    )
    assert ".download-source-version" in manifest["data"]["init-db-shard-aks.sh"]
    assert "valid_nsq_count=" in manifest["data"]["init-db-shard-aks.sh"]
    assert "printf '%s' ok > .download-complete" in manifest["data"]["init-db-shard-aks.sh"]
    assert "exit 0" in manifest["data"]["init-db-shard-aks.sh"]
    assert "vmtouch" in manifest["data"]["blast-vmtouch-aks.sh"]


def test_plan_rejects_too_few_nodes_for_one_shard_per_node() -> None:
    with pytest.raises(ValueError, match="need at least 10 nodes"):
        build_warmup_job_plan(
            db_name="core_nt",
            mol_type="nucl",
            storage_account="elbstg01",
            num_shards=10,
            nodes=_nodes(3),
            image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_name", "../core_nt"),
        ("mol_type", "rna"),
        ("storage_account", "ELBSTG01"),
        ("image", "bad image"),
    ],
)
def test_plan_rejects_unsafe_inputs(field: str, value: str) -> None:
    kwargs = {
        "db_name": "core_nt",
        "mol_type": "nucl",
        "storage_account": "elbstg01",
        "num_shards": 10,
        "nodes": _nodes(10),
        "image": "elbacr01.azurecr.io/ncbi/elb:1.4.0",
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        build_warmup_job_plan(**kwargs)


def test_database_status_from_warmup_jobs_aggregates_shards() -> None:
    jobs = [
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "00"}},
            "status": {"succeeded": 1},
        },
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "01"}},
            "status": {"active": 1},
        },
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "02"}},
            "status": {"failed": 1},
        },
    ]

    status = database_status_from_warmup_jobs(jobs)

    assert status == [
        {
            "name": "core_nt",
            "mol_type": "",
            "nodes_ready": 1,
            "nodes_failed": 1,
            "nodes_active": 1,
            "total_jobs": 3,
            "shards": ["00", "01", "02"],
            "shard_nodes": {},
            "shard_host_paths": {},
            "progress_pct": 66.7,
            "status": "Failed",
            "sources": ["warmup"],
        }
    ]


def test_database_status_from_warmup_jobs_marks_mixed_generations_stale() -> None:
    jobs = [
        {
            "metadata": {
                "labels": {"db": "core_nt", "shard": "00"},
                "annotations": {"elb.dashboard/source-version": "old"},
            },
            "status": {"succeeded": 1},
        },
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "01"}},
            "spec": {
                "template": {"metadata": {"annotations": {"elb.dashboard/source-version": "new"}}}
            },
            "status": {"succeeded": 1},
        },
    ]

    status = database_status_from_warmup_jobs(jobs)

    assert status[0]["status"] == "Stale"
    assert status[0]["source_versions"] == ["new", "old"]
    assert status[0]["active_message"] == "Warmup jobs belong to multiple DB source versions."


def test_database_status_from_warmup_jobs_aggregates_shard_named_db_labels() -> None:
    jobs = [
        {
            "metadata": {"labels": {"db": "core_nt_shard_00"}},
            "status": {"succeeded": 1},
        },
        {
            "metadata": {"labels": {"db": "core_nt_shard_01"}},
            "status": {"succeeded": 1},
        },
    ]

    status = database_status_from_warmup_jobs(jobs)

    assert len(status) == 1
    assert status[0]["name"] == "core_nt"
    assert status[0]["shards"] == ["00", "01"]


def test_database_status_from_warmup_jobs_marks_all_ready() -> None:
    jobs = [
        {
            "metadata": {"labels": {"db": "core_nt", "shard": f"{idx:02d}"}},
            "status": {"succeeded": 1},
        }
        for idx in range(10)
    ]

    status = database_status_from_warmup_jobs(jobs)

    assert status[0]["status"] == "Ready"
    assert status[0]["nodes_ready"] == 10
    assert status[0]["total_jobs"] == 10
    assert status[0]["shards"] == [f"{idx:02d}" for idx in range(10)]
    assert status[0]["progress_pct"] == 100.0


def test_database_status_from_warmup_jobs_surfaces_job_node_and_host_path() -> None:
    jobs = [
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "00"}},
            "spec": {
                "template": {
                    "spec": {
                        "nodeName": "aks-blastpool-000001",
                        "volumes": [
                            {
                                "name": "db",
                                "hostPath": {"path": "/workspace/blastdb/core_nt/00"},
                            }
                        ],
                    }
                }
            },
            "status": {"succeeded": 1},
        }
    ]

    status = database_status_from_warmup_jobs(jobs)

    assert status[0]["shard_nodes"] == {"00": "aks-blastpool-000001"}
    assert status[0]["shard_host_paths"] == {"00": "/workspace/blastdb/core_nt/00"}


def test_database_status_from_warmup_jobs_estimates_remaining_seconds() -> None:
    jobs = [
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "00"}},
            "status": {
                "succeeded": 1,
                "startTime": "2020-01-01T00:00:00Z",
                "completionTime": "2020-01-01T00:01:00Z",
            },
        },
        {
            "metadata": {"labels": {"db": "core_nt", "shard": "01"}},
            "status": {"active": 1, "startTime": "2020-01-01T00:00:00Z"},
        },
    ]

    status = database_status_from_warmup_jobs(jobs)

    assert status[0]["status"] == "Loading"
    assert status[0]["progress_pct"] == 50.0
    assert status[0]["started_at"] == "2020-01-01T00:00:00Z"
    assert status[0]["elapsed_seconds"] >= 60
    assert status[0]["estimated_remaining_seconds"] >= 60


def test_infer_warmup_pod_phase_detects_copying_from_logs() -> None:
    pod = {
        "metadata": {"name": "warm-core-nt-00-abc", "labels": {"shard": "00"}},
        "spec": {"nodeName": "aks-blast-000001"},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
        },
    }

    detail = infer_warmup_pod_phase(
        pod,
        "\n".join(
            [
                "START shard=00",
                "Downloading manifest: https://example/manifest",
                "Downloading with pattern: core_nt.*",
            ]
        ),
    )

    assert detail["phase"] == "copying_files"
    assert detail["phase_label"] == "Copying files to node disk"
    assert detail["shard"] == "00"

    def test_infer_warmup_pod_phase_treats_azcopy_retry_as_copying() -> None:
        pod = {
            "metadata": {"name": "warm-core-nt-00-x", "labels": {"shard": "00"}},
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        }

        status = infer_warmup_pod_phase(
            pod,
            "Cannot perform copy due to error: AuthorizationFailure\n"
            "azcopy attempt 2/3 failed, retrying in 10s...",
        )

        assert status["phase"] == "copying_files"
        assert status["message"] == "Storage authorization or firewall denied manifest download"

    def test_infer_warmup_pod_phase_prefers_current_progress_over_old_auth_error() -> None:
        pod = {
            "metadata": {"name": "warm-core-nt-00-x", "labels": {"shard": "00"}},
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        }

        status = infer_warmup_pod_phase(
            pod,
            "ERROR CODE: AuthorizationFailure\n"
            "Final Job Status: Completed\n"
            "Log file is located at: /root/.azcopy/current.log",
        )

        assert status["phase"] == "copying_files"
        assert status["message"] == "Log file is located at: /root/.azcopy/current.log"

    def test_infer_warmup_pod_phase_summarizes_azcopy_log_file_line() -> None:
        pod = {
            "metadata": {"name": "warm-core-nt-06-x", "labels": {"shard": "06"}},
            "spec": {"nodeName": "aks-blast-000006"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        }

        status = infer_warmup_pod_phase(
            pod,
            "Downloading with pattern: core_nt.54.*\n"
            "Log file is located at: /root/.azcopy/current.log",
        )

        assert status["phase"] == "copying_files"
        assert status["message"] == "Downloading shard files with azcopy"


    def test_infer_warmup_pod_phase_treats_azcopy_percent_as_copying() -> None:
        pod = {
            "metadata": {"name": "warm-core-nt-00-x", "labels": {"shard": "00"}},
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        }

        status = infer_warmup_pod_phase(
            pod,
            "47.1 %, 233 Done, 0 Failed, 31 Pending, 0 Skipped",
        )

        assert status["phase"] == "copying_files"
        assert status["message"] == "47.1 %, 233 Done, 0 Failed, 31 Pending, 0 Skipped"


def test_attach_pod_progress_to_database_status_adds_phase_counts() -> None:
    databases = [
        {
            "name": "core_nt",
            "mol_type": "",
            "nodes_ready": 0,
            "nodes_failed": 0,
            "nodes_active": 2,
            "total_jobs": 2,
            "shards": ["00", "01"],
            "progress_pct": 0,
            "status": "Loading",
        }
    ]
    pods = [
        {
            "metadata": {
                "name": "warm-core-nt-00-abc",
                "labels": {"db": "core_nt", "shard": "00"},
            },
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        },
        {
            "metadata": {
                "name": "warm-core-nt-01-def",
                "labels": {"db": "core_nt", "shard": "01"},
            },
            "spec": {"nodeName": "aks-blast-000002"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        },
    ]

    attach_pod_progress_to_database_status(
        databases,
        pods,
        {
            "warm-core-nt-00-abc": "Downloading with pattern: core_nt.*",
            "warm-core-nt-01-def": "vmtouch memory limit: 102G",
        },
    )

    assert databases[0]["active_phase"] == "touching_memory"
    assert databases[0]["active_phase_label"] == "Touching files into RAM"
    assert databases[0]["active_message"] == "vmtouch memory limit: 102G"
    assert databases[0]["phase_counts"] == {"copying_files": 1, "touching_memory": 1}
    assert databases[0]["progress_pct"] == 0
    assert len(databases[0]["pod_statuses"]) == 2


def test_attach_pod_progress_uses_latest_non_deleting_pod_per_shard() -> None:
    databases = [
        {
            "name": "core_nt",
            "nodes_ready": 0,
            "nodes_failed": 0,
            "nodes_active": 1,
            "total_jobs": 1,
            "status": "Loading",
        }
    ]
    pods = [
        {
            "metadata": {
                "name": "warm-core-nt-00-old",
                "creationTimestamp": "2026-05-16T16:32:45Z",
                "deletionTimestamp": "2026-05-16T16:34:00Z",
                "labels": {"db": "core_nt", "shard": "00"},
            },
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        },
        {
            "metadata": {
                "name": "warm-core-nt-00-new",
                "creationTimestamp": "2026-05-16T16:35:57Z",
                "labels": {"db": "core_nt", "shard": "00"},
            },
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        },
    ]

    attach_pod_progress_to_database_status(
        databases,
        pods,
        {
            "warm-core-nt-00-old": "AuthorizationFailure",
            "warm-core-nt-00-new": "Downloading with pattern: core_nt.*",
        },
    )

    assert databases[0]["phase_counts"] == {"copying_files": 1}
    assert databases[0]["active_message"] == "Downloading with pattern: core_nt.*"
    assert databases[0]["pod_statuses"][0]["pod"] == "warm-core-nt-00-new"


def test_attach_pod_progress_counts_log_completed_shards() -> None:
    databases = [
        {
            "name": "core_nt",
            "nodes_ready": 0,
            "nodes_failed": 0,
            "nodes_active": 2,
            "total_jobs": 2,
            "status": "Loading",
            "progress_pct": 0,
        }
    ]
    pods = [
        {
            "metadata": {
                "name": "warm-core-nt-00-a",
                "creationTimestamp": "2026-05-16T16:35:57Z",
                "labels": {"db": "core_nt", "shard": "00"},
            },
            "spec": {"nodeName": "aks-blast-000001"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        },
        {
            "metadata": {
                "name": "warm-core-nt-01-b",
                "creationTimestamp": "2026-05-16T16:35:57Z",
                "labels": {"db": "core_nt", "shard": "01"},
            },
            "spec": {"nodeName": "aks-blast-000002"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        },
    ]

    attach_pod_progress_to_database_status(
        databases,
        pods,
        {
            "warm-core-nt-00-a": "2026-05-16T16:38:04Z DONE shard=00 size=36G",
            "warm-core-nt-01-b": "Downloading with pattern: core_nt.*",
        },
    )

    assert databases[0]["nodes_ready"] == 1
    assert databases[0]["nodes_active"] == 1
    assert databases[0]["progress_pct"] == 50


def test_attach_pod_progress_uses_azcopy_percent_before_node_completes() -> None:
    databases = [
        {
            "name": "core_nt",
            "nodes_ready": 0,
            "nodes_failed": 0,
            "nodes_active": 3,
            "total_jobs": 3,
            "status": "Loading",
            "progress_pct": 0,
        }
    ]
    pods = [
        {
            "metadata": {
                "name": f"warm-core-nt-0{idx}-a",
                "creationTimestamp": "2026-05-16T16:35:57Z",
                "labels": {"db": "core_nt", "shard": f"0{idx}"},
            },
            "spec": {"nodeName": f"aks-blast-00000{idx}"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
            },
        }
        for idx in range(3)
    ]

    attach_pod_progress_to_database_status(
        databases,
        pods,
        {
            "warm-core-nt-00-a": "Running: 21.7 %, 228 Done, 0 Failed, 36 Pending",
            "warm-core-nt-01-a": "Running: 20.0 %, 210 Done, 0 Failed, 54 Pending",
            "warm-core-nt-02-a": "Running: 22.3 %, 236 Done, 0 Failed, 28 Pending",
        },
    )

    assert databases[0]["nodes_ready"] == 0
    assert databases[0]["progress_pct"] == 21.3


def test_infer_warmup_pod_phase_detects_partial_download_failure() -> None:
    pod = {
        "metadata": {"name": "warm-core-nt-06-x", "labels": {"shard": "06"}},
        "spec": {"nodeName": "aks-blast-000006"},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "warmup", "state": {"running": {}}}],
        },
    }

    status = infer_warmup_pod_phase(
        pod,
        "2026-05-16T16:38:03Z ERROR partial downloads remain: 21",
    )

    assert status["phase"] == "failed"


def test_select_warmup_shards_uses_feasible_ten_way_core_nt() -> None:
    database = {
        "name": "core_nt",
        "total_bytes": int(283.62 * 1024**3),
        "shard_sets": [1, 2, 3, 4, 5, 6, 8, 10],
    }

    selected = _select_warmup_shard_count(
        database=database,
        node_count=10,
        machine_type="Standard_E16s_v5",
    )

    assert selected == 10


def test_candidate_warmup_nodes_prefers_blastpool_ready_nodes() -> None:
    from api.services.k8s.monitoring import _candidate_warmup_node_names

    nodes = [
        {
            "metadata": {
                "name": "aks-system-000000",
                "labels": {"agentpool": "systempool", "kubernetes.azure.com/mode": "system"},
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {"name": "aks-blast-000002", "labels": {"agentpool": "blastpool"}},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        },
        {
            "metadata": {"name": "aks-blast-000001", "labels": {"agentpool": "blastpool"}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
    ]

    assert _candidate_warmup_node_names(nodes) == ["aks-blast-000001"]


class _FakeK8sResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _FakeK8sSession:
    def __init__(self) -> None:
        self.created: list[str] = []

    def get(self, url: str, *, timeout: int) -> _FakeK8sResponse:
        assert timeout == 10
        if url.endswith("/warm-core-nt-00"):
            return _FakeK8sResponse(200)
        return _FakeK8sResponse(404)

    def post(self, url: str, *, json: dict, timeout: int) -> _FakeK8sResponse:
        assert timeout == 10
        assert url.endswith("/apis/batch/v1/namespaces/default/jobs")
        self.created.append(json["metadata"]["name"])
        return _FakeK8sResponse(201)


def test_ensure_job_manifests_is_idempotent_for_existing_jobs() -> None:
    from api.services.k8s.monitoring import _ensure_job_manifests

    plan = build_warmup_job_plan(
        db_name="core_nt",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=2,
        nodes=_nodes(2),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )
    session = _FakeK8sSession()

    result = _ensure_job_manifests(session, "https://k8s.example", list(plan.jobs))

    assert result["existing"] == ["warm-core-nt-00"]
    assert result["created"] == ["warm-core-nt-01"]
    assert result["error_count"] == 0
