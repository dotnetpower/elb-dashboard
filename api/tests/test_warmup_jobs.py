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
        # By default the plan injects NO azcopy env vars, so the warmup pod
        # falls through to azcopy's own CPU-based auto-tuning.
        assert "AZCOPY_CONCURRENCY_VALUE" not in env
        assert "AZCOPY_BUFFER_GB" not in env
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
        # A cache whose volumes exist but disagree with the alias/LMDB metadata
        # ("Input db vol does not match lmdb vol") must be re-downloaded, not
        # skipped: the blastdbcmd integrity probe gates the skip decision.
        assert "CACHE_CORRUPT blastdbcmd integrity probe failed" in container["args"][0]
        assert 'blastdbcmd -db "$ELB_DB" -info' in container["args"][0]
        assert "printf '%s' ok > .download-complete" in container["args"][0]
        # The warmup pod intentionally does NOT call blast-vmtouch-aks.sh any
        # more — `azcopy` already populates the OS page cache as a side effect
        # of the download, and with no mmap holder in this pod a follow-up
        # vmtouch was a 1-second noop on already-cached pages. Pages will be
        # actively referenced by the BLAST search pod's mmap when it lands on
        # the same node. Keep the script in the ConfigMap for the
        # equivalence-experiment shell scripts that exec it directly.
        assert "blast-vmtouch-aks.sh" not in container["args"][0]
        assert "STAGING_COMPLETE shard=" in container["args"][0]


def test_plan_omits_azcopy_env_by_default_for_auto_tuning() -> None:
    # No override -> no AZCOPY_* env, so the warmup pod uses azcopy's CPU-based
    # auto-tuning (measured ~1.78x faster than the old hard-coded 16).
    plan = build_warmup_job_plan(
        db_name="core_nt",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=5,
        nodes=_nodes(5),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )
    for job in plan.jobs:
        names = {item["name"] for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert "AZCOPY_CONCURRENCY_VALUE" not in names
        assert "AZCOPY_BUFFER_GB" not in names


def test_plan_injects_azcopy_env_only_when_overridden() -> None:
    # An operator override (worker env -> plan args) is injected so azcopy
    # honours it; a None for one of them keeps that var unset.
    plan = build_warmup_job_plan(
        db_name="core_nt",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=5,
        nodes=_nodes(5),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
        azcopy_concurrency=256,
        azcopy_buffer_gb=None,
    )
    for job in plan.jobs:
        env = {
            item["name"]: item["value"]
            for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["AZCOPY_CONCURRENCY_VALUE"] == "256"
        assert "AZCOPY_BUFFER_GB" not in env


def test_plan_rejects_out_of_range_azcopy_override() -> None:
    with pytest.raises(ValueError, match="azcopy_concurrency"):
        build_warmup_job_plan(
            db_name="core_nt",
            mol_type="nucl",
            storage_account="elbstg01",
            num_shards=1,
            nodes=_nodes(1),
            image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
            azcopy_concurrency=99999,
        )


def test_env_int_override_falls_back_to_default(monkeypatch) -> None:
    # The warmup task's ops-override reader must degrade to None (= "let azcopy
    # auto-tune") for every bad input so a typo never fails the warmup, rather
    # than passing an out-of-range value to the plan's range check.
    from api.tasks.storage.warmup import _env_int_override

    name = "WARMUP_AZCOPY_CONCURRENCY"
    # unset
    monkeypatch.delenv(name, raising=False)
    assert _env_int_override(name, lo=1, hi=512) is None
    # empty / non-numeric / non-positive / out-of-range all fall back to None
    for bad in ("", "   ", "abc", "0", "-5", "99999"):
        monkeypatch.setenv(name, bad)
        assert _env_int_override(name, lo=1, hi=512) is None, bad
    # a valid in-range value is honoured
    monkeypatch.setenv(name, "256")
    assert _env_int_override(name, lo=1, hi=512) == 256


def test_warmup_azcopy_concurrency_uses_bounded_default_and_override(monkeypatch) -> None:
    from api.tasks.storage.warmup import _warmup_azcopy_concurrency

    monkeypatch.delenv("WARMUP_AZCOPY_CONCURRENCY", raising=False)
    assert _warmup_azcopy_concurrency() == 64

    monkeypatch.setenv("WARMUP_AZCOPY_CONCURRENCY", "32")
    assert _warmup_azcopy_concurrency() == 32

    monkeypatch.setenv("WARMUP_AZCOPY_CONCURRENCY", "invalid")
    assert _warmup_azcopy_concurrency() == 64


def test_single_shard_db_is_broadcast_to_every_node() -> None:
    # A single-shard DB is the full database. The search batch can land on any
    # workload=blast node, so the full DB must be staged on every Ready node —
    # not just node 0. Regression for blastn exit 2 ("BLAST database not found")
    # when the search lands on an un-warmed node.
    plan = build_warmup_job_plan(
        db_name="16S_ribosomal_RNA",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=1,
        nodes=_nodes(9),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )

    # One Job per node, each pinned to a distinct node.
    assert len(plan.jobs) == 9
    assert plan.nodes == tuple(_nodes(9))
    pinned_nodes = [job["spec"]["template"]["spec"]["nodeName"] for job in plan.jobs]
    assert pinned_nodes == _nodes(9)

    names = [job["metadata"]["name"] for job in plan.jobs]
    # Names are unique (one per node ordinal) so all Jobs can coexist.
    assert len(set(names)) == 9
    assert names[0] == "warm-16s-ribosomal-rna-00"
    assert names[8] == "warm-16s-ribosomal-rna-08"

    for job in plan.jobs:
        env = {
            item["name"]: item["value"]
            for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        # Every node stages the same full-DB (shard-00) content.
        assert env["ELB_DB"] == "16S_ribosomal_RNA_shard_00"
        assert env["ELB_SHARD_IDX"] == "00"


def test_warmup_jobs_carry_hang_backstop_deadline() -> None:
    # Every warmup Job must set activeDeadlineSeconds so a hung azcopy cannot
    # leave the pod Running forever (default 1 h, far above any real warmup).
    plan = build_warmup_job_plan(
        db_name="core_nt",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=10,
        nodes=_nodes(10),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )
    for job in plan.jobs:
        assert job["spec"]["activeDeadlineSeconds"] == 3600
        assert job["spec"]["backoffLimit"] == 1


def test_warmup_job_deadline_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BLAST_WARMUP_JOB_DEADLINE_SECONDS", "1800")
    plan = build_warmup_job_plan(
        db_name="16S_ribosomal_RNA",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=1,
        nodes=_nodes(1),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )
    assert plan.jobs
    for job in plan.jobs:
        assert job["spec"]["activeDeadlineSeconds"] == 1800


def test_single_shard_single_node_keeps_one_job() -> None:
    # A 1-node cluster keeps the original single-Job placement.
    plan = build_warmup_job_plan(
        db_name="16S_ribosomal_RNA",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=1,
        nodes=_nodes(1),
        image="elbacr01.azurecr.io/ncbi/elb:1.4.0",
    )

    assert len(plan.jobs) == 1
    assert plan.jobs[0]["metadata"]["name"] == "warm-16s-ribosomal-rna-00"


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
    assert "--overwrite=ifSourceNewer" in manifest["data"]["init-db-shard-aks.sh"]
    # The reusable script must not pin policy itself. The production task
    # injects bounded concurrency through the Job environment; direct builder
    # benchmarks can still omit it to exercise azcopy auto-tuning.
    assert (
        "AZCOPY_CONCURRENCY_VALUE=${AZCOPY_CONCURRENCY_VALUE:-16}"
        not in (manifest["data"]["init-db-shard-aks.sh"])
    )
    assert (
        "AZCOPY_BUFFER_GB=${AZCOPY_BUFFER_GB:-2}" not in (manifest["data"]["init-db-shard-aks.sh"])
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
            "status": {
                "failed": 1,
                "conditions": [{"type": "Failed", "status": "True"}],
            },
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


def test_database_status_does_not_fail_while_job_retry_is_active() -> None:
    status = database_status_from_warmup_jobs(
        [
            {
                "metadata": {"labels": {"db": "core_nt", "shard": "00"}},
                "status": {"failed": 1, "active": 1, "conditions": []},
            }
        ]
    )

    assert status[0]["status"] == "Loading"
    assert status[0]["nodes_failed"] == 0
    assert status[0]["nodes_active"] == 1


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


def test_attach_pod_progress_post_copy_phases_do_not_regress_to_zero() -> None:
    # Regression guard for the progress-bar saw-tooth: once a shard finishes
    # copying and moves to verifying the local DB or touching it into RAM, its
    # logs no longer contain an azcopy "%". The aggregate must treat those
    # post-copy phases as 100, not fall back to 0.
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
            # blastdbcmd / "database:" markers → verifying_db (post-copy).
            "warm-core-nt-00-a": "blastdbcmd -info -db core_nt",
            # vmtouch marker → touching_memory (post-copy).
            "warm-core-nt-01-b": "vmtouch memory limit: 102G",
        },
    )

    assert databases[0]["phase_counts"] == {
        "verifying_db": 1,
        "touching_memory": 1,
    }
    assert databases[0]["progress_pct"] == 100.0


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
    # The `touching_memory` shard already finished copying, so it counts as 100;
    # the still-copying shard has no azcopy "%" yet and counts as 0. Aggregate
    # is (0 + 100) / 2 = 50 — post-copy phases must not regress to 0 (that was
    # the source of the progress-bar saw-tooth).
    assert databases[0]["progress_pct"] == 50.0
    assert len(databases[0]["pod_statuses"]) == 2


def test_attach_pod_progress_preserves_failed_jobs_when_failed_pods_are_gone() -> None:
    jobs = []
    pods = []
    logs: dict[str, str] = {}
    for index in range(10):
        labels = {"db": "core_nt", "shard": f"{index:02d}"}
        jobs.append(
            {
                "metadata": {"name": f"warm-core-nt-{index:02d}", "labels": labels},
                "status": (
                    {"succeeded": 1}
                    if index < 7
                    else {
                        "failed": 1,
                        "conditions": [
                            {
                                "type": "Failed",
                                "status": "True",
                                "reason": "DeadlineExceeded",
                            }
                        ],
                    }
                ),
            }
        )
        if index < 7:
            pod_name = f"warm-core-nt-{index:02d}-pod"
            pods.append(
                {
                    "metadata": {"name": pod_name, "labels": labels},
                    "status": {
                        "phase": "Succeeded",
                        "containerStatuses": [
                            {
                                "name": "warmup",
                                "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
                            }
                        ],
                    },
                }
            )
            logs[pod_name] = f"DONE shard={index:02d}"

    databases = database_status_from_warmup_jobs(jobs)
    attach_pod_progress_to_database_status(databases, pods, logs)

    assert databases[0]["status"] == "Failed"
    assert databases[0]["nodes_ready"] == 7
    assert databases[0]["nodes_failed"] == 3
    assert databases[0]["nodes_active"] == 0
    assert databases[0]["progress_pct"] == 100.0
    assert databases[0]["active_phase"] == "failed"
    assert "pod details are no longer available" in databases[0]["active_message"]


def test_new_staging_complete_log_resolves_to_completed_phase() -> None:
    # The new warmup pod no longer runs `/scripts/blast-vmtouch-aks.sh`, so its
    # logs never contain "vmtouch memory limit" / "cache-blastdbs-to-ram". The
    # final two lines are STAGING_COMPLETE + DONE; the existing "done shard="
    # matcher in `_phase_from_warmup_log` must keep classifying that as the
    # terminal "completed" phase.
    databases = [
        {
            "name": "core_nt",
            "nodes_ready": 0,
            "nodes_failed": 0,
            "nodes_active": 1,
            "total_jobs": 1,
            "shards": ["00"],
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
    ]
    log_text = (
        "2026-06-06T00:00:00Z DOWNLOAD_SKIP existing shard=00\n"
        "Database: core_nt\n"
        "2026-06-06T00:00:01Z STAGING_COMPLETE shard=00\n"
        "2026-06-06T00:00:01Z DONE shard=00 size=36G\n"
    )
    attach_pod_progress_to_database_status(
        databases,
        pods,
        {"warm-core-nt-00-abc": log_text},
    )

    detail = databases[0]["pod_statuses"][0]
    assert detail["phase"] == "completed"
    assert databases[0]["active_phase"] == "completed"


def test_warmup_skip_path_warms_page_cache_with_vmtouch() -> None:
    """On the DOWNLOAD_SKIP path (node_disk / data_disk restart where the shard
    survived on the node disk and azcopy was skipped), the warmup entrypoint
    must read the shard into the node page cache so the first search does not
    pay the full disk->RAM fault cost. The vmtouch must live ONLY in the skip
    branch (the download branch already warms the cache as a side effect)."""
    from api.services.warmup.scripts import warmup_shell_command

    script = warmup_shell_command()
    # The skip-path warm step is present and opt-out-able.
    assert "VMTOUCH_WARM shard=" in script
    assert "vmtouch -tqm" in script
    assert "ELB_WARMUP_VMTOUCH_DISABLE" in script
    assert "-getvolumespath" in script
    # It is inside the DOWNLOAD_SKIP branch (after the skip marker), not the
    # download branch — a warm cache must not pay a redundant vmtouch.
    assert script.index("DOWNLOAD_SKIP existing") < script.index("VMTOUCH_WARM shard=")
    # It is best-effort so a vmtouch failure never fails staging.
    assert "|| true" in script.split("VMTOUCH_WARM shard=")[1].split("RUNTIME vmtouch-warm")[0]
    # The container entrypoint still does not call the retired ConfigMap script.
    assert "blast-vmtouch-aks.sh" not in script


def test_warmup_skip_path_logs_when_vmtouch_unavailable() -> None:
    """If the warmup image lacks vmtouch/blastdb_path the warm is impossible;
    the entrypoint must LOG that (observable no-op) rather than skip silently,
    and must also log when disabled or when volume paths cannot be resolved."""
    from api.services.warmup.scripts import warmup_shell_command

    script = warmup_shell_command()
    assert "VMTOUCH_SKIP vmtouch/blastdb_path not available" in script
    assert "VMTOUCH_SKIP disabled via ELB_WARMUP_VMTOUCH_DISABLE" in script
    assert "VMTOUCH_SKIP could not resolve volume paths" in script


def test_warmup_skip_path_budget_has_floor_and_fallback() -> None:
    """The vmtouch budget must never degrade to a silent `-m 0G` / `-m ''` noop:
    the awk floors to >=1G and there is a fixed fallback when MemAvailable is
    absent."""
    from api.services.warmup.scripts import warmup_shell_command

    script = warmup_shell_command()
    assert "if (mb<1) mb=1" in script or "-ge 1" in script
    assert "vm_gib=4" in script
    # The path resolution is guarded so an empty volume list does not run
    # vmtouch with no args and is logged instead.
    assert 'if [ -n "$vm_paths" ]; then' in script


def test_vmtouch_warm_log_maps_to_touching_memory_phase() -> None:
    """A warmup pod still reading the shard into RAM (VMTOUCH_WARM emitted, no
    DONE yet) reports the `touching_memory` phase, and a completed pod that also
    emitted VMTOUCH_WARM still resolves to `completed` (the `done shard=`
    matcher has priority)."""
    from api.services.warmup.jobs import _phase_from_warmup_log

    in_flight = (
        "2026-07-22T00:00:00Z DOWNLOAD_SKIP existing shard=00\n"
        "2026-07-22T00:00:00Z VMTOUCH_WARM shard=00 db=core_nt budget=98G\n"
    )
    assert _phase_from_warmup_log(in_flight) == "touching_memory"

    completed = in_flight + (
        "2026-07-22T00:01:30Z RUNTIME vmtouch-warm-shard-00 90 seconds\n"
        "2026-07-22T00:01:31Z STAGING_COMPLETE shard=00\n"
        "2026-07-22T00:01:31Z DONE shard=00 size=36G\n"
    )
    assert _phase_from_warmup_log(completed) == "completed"


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


def test_candidate_warmup_nodes_excludes_system_only_cluster() -> None:
    """Node selection must never fall back to system-pool nodes: a Job pinned to
    a `CriticalAddonsOnly`-tainted system node stays Pending forever. A cluster
    with only system nodes returns an empty candidate list so the caller defers
    instead of placing doomed Jobs."""
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
            "metadata": {
                "name": "aks-system-000001",
                "labels": {"agentpool": "system", "kubernetes.azure.com/mode": "system"},
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
    ]

    assert _candidate_warmup_node_names(nodes) == []


def test_broadcast_single_shard_db_uses_per_node_job_count() -> None:
    """A single-shard DB broadcast across N nodes must produce N Jobs (one per
    node), so the warmup wait target is N — not 1. Waiting on `selected_shards`
    (==1) would report the DB warm after only one node finished."""
    from api.services.warmup.jobs import build_warmup_job_plan

    plan = build_warmup_job_plan(
        db_name="16S_ribosomal_RNA",
        mol_type="nucl",
        storage_account="elbstg01",
        num_shards=1,
        nodes=["node-a", "node-b", "node-c"],
        image="acr.azurecr.io/ncbi/elb:latest",
    )
    # Broadcast → one Job per node, not one Job for the single shard.
    assert len(plan.jobs) == 3
    # The warmup task derives expected_jobs from len(plan.jobs); confirm the
    # broadcast count is what the wait would target.
    assert max(1, len(plan.jobs)) == 3
