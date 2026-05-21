"""Tests for BLAST Tasks behavior.

Responsibility: Tests for BLAST Tasks behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `FakeK8sResponse`, `FakeK8sSession`, `_parse_ini`,
`test_build_config_content_targets_existing_cluster_and_storage_urls`,
`test_build_config_content_preserves_full_blob_urls`,
`test_build_config_content_rejects_relative_path_traversal`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import configparser
import gzip
import io
import json
import xml.etree.ElementTree as ET

import pytest
from api._http_utils import BlastSubmitRequest
from api.services.query_grouping import build_query_split_execution_plan
from api.services.query_metadata import parse_fasta_metadata
from api.tasks import blast
from azure.core.exceptions import ResourceNotFoundError
from fastapi import HTTPException
from pydantic import ValidationError


class FakeK8sResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, object]:
        return {}


class FakeK8sSession:
    def __init__(self) -> None:
        self.deletes: list[dict[str, object]] = []
        self.closed = False

    def get(self, _url: str, *, timeout: int) -> FakeK8sResponse:
        assert timeout == 10
        return FakeK8sResponse(200)

    def delete(self, url: str, *, params: dict[str, str], timeout: int) -> FakeK8sResponse:
        assert timeout == 10
        self.deletes.append({"url": url, "params": params})
        return FakeK8sResponse(200)

    def close(self) -> None:
        self.closed = True


def _parse_ini(content: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_file(io.StringIO(content))
    return parser


def test_build_config_content_targets_existing_cluster_and_storage_urls() -> None:
    content = blast._build_config_content(
        job_id="job-123",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        storage_account="stelb",
        program="blastn",
        database="pdbnt",
        query_file="queries/input.fa",
        options={
            "acr_name": "elbacr01",
            "acr_resource_group": "rg-elbacr-01",
            "machine_type": "Standard_E32s_v5",
            "num_nodes": 2,
        },
    )

    cfg = _parse_ini(content)

    assert cfg.get("cluster", "name") == "aks-elb"
    assert cfg.get("cluster", "machine-type") == "Standard_E32s_v5"
    assert cfg.get("cluster", "num-nodes") == "2"
    assert cfg.get("cluster", "reuse") == "true"
    assert cfg.get("cloud-provider", "azure-acr-name") == "elbacr01"
    assert cfg.get("cloud-provider", "azure-acr-resource-group") == "rg-elbacr-01"
    assert cfg.get("blast", "db") == "https://stelb.blob.core.windows.net/blast-db/pdbnt/pdbnt"
    assert cfg.get("blast", "queries") == "https://stelb.blob.core.windows.net/queries/input.fa"
    assert cfg.get("blast", "results") == "https://stelb.blob.core.windows.net/results/job-123"
    assert cfg.get("cloud-provider", "azure-storage-account-container") == "blast-db"


def test_build_config_content_preserves_full_blob_urls() -> None:
    content = blast._build_config_content(
        job_id="job-123",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        storage_account="stelb",
        database="https://stelb.blob.core.windows.net/blast-db/custom/mydb",
        query_file="https://stelb.blob.core.windows.net/queries/custom.fa",
    )

    cfg = _parse_ini(content)

    assert cfg.get("blast", "db") == "https://stelb.blob.core.windows.net/blast-db/custom/mydb"
    assert cfg.get("blast", "queries") == "https://stelb.blob.core.windows.net/queries/custom.fa"


def test_build_config_content_rejects_mismatched_storage_blob_urls() -> None:
    with pytest.raises(ValueError, match="database URL must belong"):
        blast._build_config_content(
            job_id="job-123",
            resource_group="rg-elb",
            cluster_name="aks-elb",
            storage_account="elbstg01",
            database="https://stgelb.blob.core.windows.net/blast-db/custom/mydb",
            query_file="queries/custom.fa",
        )

    with pytest.raises(ValueError, match="query_file URL must belong"):
        blast._build_config_content(
            job_id="job-123",
            resource_group="rg-elb",
            cluster_name="aks-elb",
            storage_account="elbstg01",
            database="core_nt",
            query_file="https://stgelb.blob.core.windows.net/queries/custom.fa",
        )


@pytest.mark.parametrize(
    ("database", "query_file", "match"),
    [
        (
            "https://elbstg01.blob.core.windows.net/other/custom/mydb",
            "queries/custom.fa",
            "database URL must point to the blast-db container",
        ),
        (
            "https://elbstg01.blob.core.windows.net/blast-db/custom/mydb?sig=bad",
            "queries/custom.fa",
            "database URL must not include query strings",
        ),
        (
            "core_nt",
            "https://elbstg01.blob.core.windows.net/results/custom.fa",
            "query_file URL must point to the queries container",
        ),
        (
            "core_nt",
            "http://elbstg01.blob.core.windows.net/queries/custom.fa",
            "query_file URL must use https",
        ),
    ],
)
def test_build_config_content_rejects_unsafe_absolute_urls(
    database: str,
    query_file: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        blast._build_config_content(
            job_id="job-123",
            resource_group="rg-elb",
            cluster_name="aks-elb",
            storage_account="elbstg01",
            database=database,
            query_file=query_file,
        )


def test_blast_submit_request_rejects_storage_url_account_mismatch() -> None:
    with pytest.raises(ValidationError, match="database URL must belong"):
        BlastSubmitRequest(
            resource_group="rg-elb",
            cluster_name="aks-elb",
            storage_account="elbstg01",
            program="blastn",
            database="https://stgelb.blob.core.windows.net/blast-db/custom/mydb",
            query_file="queries/custom.fa",
        )


def test_build_config_content_rejects_relative_path_traversal() -> None:
    with pytest.raises(ValueError, match="query_file"):
        blast._build_config_content(
            job_id="job-123",
            resource_group="rg-elb",
            cluster_name="aks-elb",
            storage_account="stelb",
            database="pdbnt",
            query_file="../input.fa",
        )


def test_elastic_blast_argv_uses_cfg_file() -> None:
    argv = blast._elastic_blast_argv("submit", "abc-123")

    assert argv == ["elastic-blast", "submit", "--cfg", "elastic-blast.ini"]
    assert "--json" not in argv
    assert "--idempotency-key" not in argv
    assert "bash" not in argv


def test_last_json_reads_structured_payload_from_log_tail() -> None:
    payload = blast._last_json('info line\n{"kind":"submit_result","decision":"accepted"}\n')

    assert payload == {"kind": "submit_result", "decision": "accepted"}


def test_retryable_result_uses_structured_category_and_exit_code() -> None:
    assert blast._is_retryable_result({"exit_code": 1}, {"kind": "error", "category": "capacity"})
    assert blast._is_retryable_result({"exit_code": 8}, None)
    assert not blast._is_retryable_result(
        {"exit_code": 1}, {"kind": "error", "category": "invalid"}
    )


def test_update_state_uses_repository_contract(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.updates: list[tuple[str, dict[str, object]]] = []
            self.history: list[tuple[str, str, dict[str, object]]] = []

        def update(self, job_id: str, **kwargs: object) -> None:
            self.updates.append((job_id, kwargs))

        def append_history(self, job_id: str, event: str, payload: dict[str, object]) -> None:
            self.history.append((job_id, event, payload))

    repo = FakeRepo()
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)

    blast._update_state(
        "job-123",
        "submitted",
        status="running",
        error_code=None,
        decision="accepted",
    )

    assert repo.updates == [
        (
            "job-123",
            {"status": "running", "phase": "submitted", "error_code": ""},
        )
    ]
    assert repo.history[0][0] == "job-123"
    assert repo.history[0][1] == "submitted"
    assert repo.history[0][2]["decision"] == "accepted"


def test_stream_submit_command_defers_log_artifact_writes(monkeypatch) -> None:
    def fake_stream(**_kwargs: object):
        yield {"stream": "stdout", "line": "line one"}
        yield {"stream": "stderr", "line": "line two"}
        yield {"exit_code": 0, "duration_ms": 123, "timed_out": False}

    class FakeTask:
        def __init__(self) -> None:
            self.states: list[dict[str, object]] = []

        def update_state(self, *, state: str, meta: dict[str, object]) -> None:
            self.states.append({"state": state, "meta": meta})

    artifact_calls: list[tuple[object, ...]] = []
    state_updates: list[tuple[tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr("api.services.terminal_exec.stream", fake_stream)
    monkeypatch.setattr(
        "api.services.job_artifacts.write_execution_log_chunk",
        lambda *args: artifact_calls.append(args),
    )
    monkeypatch.setattr(
        "api.services.job_logs.event_bus.publish_job_log_event",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda *args, **kwargs: state_updates.append((args, kwargs)),
    )

    result = blast._stream_submit_command(
        job_id="job-123",
        task=FakeTask(),
        config_content="[cluster]\n",
    )

    assert artifact_calls == []
    assert result["exit_code"] == 0
    assert result["stdout"] == "line one"
    assert result["stderr"] == "line two"
    assert result["_log_events"] == [
        {"stream": "stdout", "line": "line one", "index": 1},
        {"stream": "stderr", "line": "line two", "index": 2},
    ]
    assert state_updates[0][0][:3] == ("job-123", "submitting")


def test_persist_submit_log_events_chunks_after_stream(monkeypatch) -> None:
    calls: list[tuple[str, str, int, list[dict[str, object]]]] = []

    def fake_write(
        job_id: str,
        step_key: str,
        sequence: int,
        events: list[dict[str, object]],
    ) -> None:
        calls.append((job_id, step_key, sequence, events))

    monkeypatch.setattr("api.services.job_artifacts.write_execution_log_chunk", fake_write)
    events = [
        {"stream": "stdout", "line": f"line {index}", "index": index}
        for index in range(blast.SUBMIT_LOG_CHUNK_EVENT_COUNT + 1)
    ]

    blast._persist_submit_log_events(
        job_id="job-123",
        progress_phase="submitting",
        events=events,
    )

    assert [(call[0], call[1], call[2], len(call[3])) for call in calls] == [
        ("job-123", "submitting", 0, blast.SUBMIT_LOG_CHUNK_EVENT_COUNT),
        ("job-123", "submitting", 1, 1),
    ]


def test_k8s_cancel_blast_job_deletes_only_scoped_jobs(monkeypatch) -> None:
    from api.services import k8s_monitoring, monitoring

    session = FakeK8sSession()
    monkeypatch.setattr(
        k8s_monitoring,
        "_get_k8s_session",
        lambda *_args: (session, "https://k8s.example"),
    )

    result = monitoring.k8s_cancel_blast_job(
        object(),
        "sub",
        "rg-elb",
        "aks-elb",
        "default",
        "job-123",
    )

    assert result["status"] == "cancelled"
    assert session.closed is True
    assert [delete["params"]["labelSelector"] for delete in session.deletes] == [
        "app=blast,elb-job-id=job-123",
        "app=submit,elb-job-id=job-123",
    ]


def test_k8s_cancel_blast_job_rejects_invalid_label_value() -> None:
    from api.services.monitoring import k8s_cancel_blast_job

    with pytest.raises(ValueError, match="job_id"):
        k8s_cancel_blast_job(object(), "sub", "rg", "aks", "default", "bad/job")


def test_build_config_auto_resolves_metadata_without_sharding_but_uses_local_ssd(
    monkeypatch,
) -> None:
    # Fake metadata.json contents the prepare-db pipeline would have written.
    fake_meta = {
        "db_name": "core_nt",
        "sharded": True,
        "shard_sets": [1, 2, 3, 4, 5, 6, 8, 10],
        "total_bytes": 269 * 1024**3,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={"machine_type": "Standard_E16s_v5", "num_nodes": 5},
    )
    cfg = _parse_ini(content)
    assert not cfg.has_option("blast", "db-partitions")
    assert not cfg.has_option("blast", "db-partition-prefix")
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"


def test_build_config_approximate_sharding_opt_in_injects_partitions(monkeypatch) -> None:
    # Fake metadata.json contents the prepare-db pipeline would have written.
    fake_meta = {
        "db_name": "core_nt",
        "sharded": True,
        "shard_sets": [1, 2, 3, 4, 5, 6, 8, 10],
        "total_bytes": 269 * 1024**3,
        "total_letters": 123_456_789,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 5,
            "allow_approximate_sharding": True,
        },
    )
    cfg = _parse_ini(content)
    # 269 GB on E16 (128 GB) → memory floor 5; nodes=5 → preset 5
    assert cfg.get("blast", "db-partitions") == "5"
    assert cfg.get("blast", "db-partition-prefix") == (
        "https://elbstg01.blob.core.windows.net/blast-db/5shards/core_nt_shard_"
    )
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"
    assert "-dbsize 123456789" in cfg.get("blast", "options")


def test_build_config_non_core_prepared_metadata_can_inject_partitions(monkeypatch) -> None:
    fake_meta = {
        "db_name": "16S_ribosomal_RNA",
        "source_version": "2026-05-20-00-00-00",
        "sharded": True,
        "shard_sets": [1, 5],
        "shard_source_version": "2026-05-20-00-00-00",
        "total_bytes": 269 * 1024**3,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="16S_ribosomal_RNA",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 5,
            "allow_approximate_sharding": True,
        },
    )
    cfg = _parse_ini(content)
    assert cfg.get("blast", "db-partitions") == "5"
    assert cfg.get("blast", "db-partition-prefix") == (
        "https://elbstg01.blob.core.windows.net/blast-db/5shards/16S_ribosomal_RNA_shard_"
    )


def test_build_config_metadata_effective_search_space_injects_searchsp(monkeypatch) -> None:
    fake_meta = {
        "db_name": "core_nt",
        "sharded": True,
        "shard_sets": [1, 2, 3, 4, 5, 6, 8, 10],
        "total_bytes": 269 * 1024**3,
        "total_letters": 123_456_789,
        "effective_search_space": 2_254_169_736,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 5,
            "allow_approximate_sharding": True,
        },
    )
    options = _parse_ini(content).get("blast", "options")
    assert "-searchsp 2254169736" in options
    assert "-dbsize" not in options


def test_build_config_core_nt_calibrated_search_space_injects_searchsp(monkeypatch) -> None:
    calibrated_search_space = 32_156_241_807_668

    fake_meta = {
        "db_name": "core_nt",
        "sharded": True,
        "shard_sets": [10],
        "total_bytes": 269 * 1024**3,
        "total_letters": 1_041_443_571_674,
        "effective_search_space": calibrated_search_space,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 10,
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_count": 1,
        },
    )

    options = _parse_ini(content).get("blast", "options")
    assert "-searchsp 32156241807668" in options
    assert "-dbsize" not in options


def test_node_warmup_ready_check_allows_ready_sharded_submit(monkeypatch) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_warmup_status(*_args, **_kwargs):
        return {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Ready",
                    "nodes_ready": 10,
                    "total_jobs": 10,
                }
            ]
        }

    monkeypatch.setattr("api.services.monitoring.k8s_warmup_status", fake_warmup_status)

    ready = blast._ensure_node_warmup_ready_for_submit(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        database="core_nt",
        options={"sharding_mode": "precise", "enable_warmup": True},
    )

    assert ready is not None
    assert ready["status"] == "Ready"


def test_node_warmup_ready_check_rejects_stale_warm_generation(monkeypatch) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        blast,
        "resolve_db_metadata",
        lambda *_args, **_kwargs: {
            "db_name": "core_nt",
            "source_version": "2026-05-20-00-00-00",
            "sharded": True,
            "shard_sets": [10],
            "shard_source_version": "2026-05-20-00-00-00",
        },
    )
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *_args, **_kwargs: {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Ready",
                    "source_version": "2026-05-19-00-00-00",
                    "nodes_ready": 10,
                    "total_jobs": 10,
                }
            ]
        },
    )

    with pytest.raises(blast.WarmupNotReadyError) as err:
        blast._ensure_node_warmup_ready_for_submit(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            database="core_nt",
            storage_account="elbstg01",
            options={"sharding_mode": "precise", "enable_warmup": True},
        )

    assert err.value.retryable is True
    assert "stale DB generation" in str(err.value)


def test_node_warmup_ready_check_retries_loading_sharded_submit(monkeypatch) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.k8s_warmup_status",
        lambda *_a, **_k: {
            "databases": [
                {
                    "name": "core_nt",
                    "status": "Loading",
                    "nodes_ready": 6,
                    "nodes_active": 4,
                    "total_jobs": 10,
                }
            ]
        },
    )

    with pytest.raises(blast.WarmupNotReadyError) as err:
        blast._ensure_node_warmup_ready_for_submit(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            database="core_nt",
            options={"db_auto_partition": True, "enable_warmup": True},
        )

    assert err.value.retryable is True
    assert "6/10" in str(err.value)


def test_node_warmup_ready_check_skips_unsharded_submit() -> None:
    assert (
        blast._ensure_node_warmup_ready_for_submit(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            database="core_nt",
            options={"sharding_mode": "off", "enable_warmup": True},
        )
        is None
    )


def test_gate_completed_submit_waits_for_result_artifacts(monkeypatch) -> None:
    monkeypatch.setattr(blast, "_has_parseable_result_artifact", lambda *_args: False)

    assert blast._gate_completed_submit_on_results(
        job_id="job-1",
        storage_account="stelb",
        phase="completed",
        status="completed",
    ) == ("results_pending", "running")


def test_gate_completed_submit_allows_completed_with_result_artifacts(monkeypatch) -> None:
    monkeypatch.setattr(blast, "_has_parseable_result_artifact", lambda *_args: True)

    assert blast._gate_completed_submit_on_results(
        job_id="job-1",
        storage_account="stelb",
        phase="completed",
        status="completed",
    ) == ("completed", "completed")


def test_node_warmup_ready_check_skips_stale_sharded_options_for_unsharded_db(
    monkeypatch,
) -> None:
    fake_meta = {
        "db_name": "18S_fungal_sequences",
        "sharded": False,
        "shard_sets": [],
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)

    def fail_warmup_status(*_args, **_kwargs):
        raise AssertionError("warmup status should not be called for unsharded DB")

    monkeypatch.setattr("api.services.monitoring.k8s_warmup_status", fail_warmup_status)

    assert (
        blast._ensure_node_warmup_ready_for_submit(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            database="18S_fungal_sequences",
            storage_account="elbstg01",
            options={"sharding_mode": "precise", "enable_warmup": True},
        )
        is None
    )


def test_build_config_skips_auto_shard_when_metadata_missing(monkeypatch) -> None:
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: None)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={"machine_type": "Standard_E16s_v5", "num_nodes": 5},
    )
    cfg = _parse_ini(content)
    assert not cfg.has_option("blast", "db-partitions")


def test_build_config_forces_sharding_off_when_metadata_is_unsharded(monkeypatch) -> None:
    fake_meta = {
        "db_name": "18S_fungal_sequences",
        "sharded": False,
        "shard_sets": [],
        "total_bytes": 2 * 1024**3,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="18S_fungal_sequences",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 10,
            "db_auto_partition": True,
            "db_sharded": True,
            "db_partitions": 10,
            "db_partition_prefix": "https://elbstg01.blob.core.windows.net/blast-db/10shards/18S_fungal_sequences_shard_",
            "sharding_mode": "precise",
            "outfmt": 6,
        },
    )
    cfg = _parse_ini(content)
    assert not cfg.has_option("blast", "db-partitions")
    assert not cfg.has_option("blast", "db-partition-prefix")


def test_unsharded_metadata_suppresses_stale_split_sharding_options(monkeypatch) -> None:
    fake_meta = {
        "db_name": "18S_fungal_sequences",
        "sharded": False,
        "shard_sets": [],
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    options = blast._suppress_sharding_for_unsharded_database(
        storage_account="elbstg01",
        database="18S_fungal_sequences",
        options={
            "sharding_mode": "precise",
            "query_count": 2,
            "query_effective_search_spaces": [100, 200],
            "outfmt": 6,
        },
    )
    assert options is not None
    assert options["sharding_mode"] == "off"
    assert options["db_auto_partition"] is False
    assert blast._requires_split_parent_submission(options) is False


def test_single_part_non_core_nt_metadata_suppresses_sharding_options(monkeypatch) -> None:
    fake_meta = {
        "db_name": "18S_fungal_sequences",
        "sharded": True,
        "shard_sets": [1],
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    options = blast._suppress_sharding_for_unsharded_database(
        storage_account="elbstg01",
        database="18S_fungal_sequences",
        options={
            "sharding_mode": "precise",
            "db_auto_partition": True,
            "db_sharded": True,
            "shard_sets": [1],
            "outfmt": 6,
        },
    )
    assert options is not None
    assert options["sharding_mode"] == "off"
    assert options["db_auto_partition"] is False
    assert options["db_sharded"] is False


def test_stale_shard_generation_suppresses_sharding_options(monkeypatch) -> None:
    fake_meta = {
        "db_name": "16S_ribosomal_RNA",
        "source_version": "2026-05-20-00-00-00",
        "sharded": True,
        "shard_sets": [5],
        "shard_source_version": "2026-05-19-00-00-00",
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    options = blast._suppress_sharding_for_unsharded_database(
        storage_account="elbstg01",
        database="16S_ribosomal_RNA",
        options={
            "sharding_mode": "precise",
            "db_auto_partition": True,
            "db_sharded": True,
            "shard_sets": [5],
            "outfmt": 6,
        },
    )
    assert options is not None
    assert options["sharding_mode"] == "off"
    assert options["db_auto_partition"] is False
    assert options["db_sharded"] is False


def test_build_config_caller_provided_metadata_wins(monkeypatch) -> None:
    # Storage metadata is still resolved when the caller passes a coarse
    # db_sharded flag, but explicit caller values must not be overwritten.
    called = []
    monkeypatch.setattr(
        blast,
        "resolve_db_metadata",
        lambda *a, **k: (
            called.append(1),
            {
                "db_name": "core_nt",
                "sharded": True,
                "total_bytes": 269 * 1024**3,
            },
        )[1],
    )
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 5,
            "db_sharded": False,  # caller asserts not sharded
        },
    )
    cfg = _parse_ini(content)
    assert called == [1]
    assert not cfg.has_option("blast", "db-partitions")


def test_build_config_db_sharded_flag_still_resolves_missing_metadata(monkeypatch) -> None:
    fake_meta = {
        "db_name": "core_nt",
        "sharded": True,
        "shard_sets": [10],
        "total_bytes": 269 * 1024**3,
        "total_letters": 1_041_443_571_674,
        "effective_search_space": 32_156_241_807_668,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 10,
            "db_sharded": True,
            "sharding_mode": "precise",
            "query_count": 1,
            "outfmt": 5,
        },
    )
    cfg = _parse_ini(content)
    assert cfg.get("blast", "db-partitions") == "10"
    assert cfg.get("blast", "db-partition-prefix") == (
        "https://elbstg01.blob.core.windows.net/blast-db/10shards/core_nt_shard_"
    )
    assert "-searchsp 32156241807668" in cfg.get("blast", "options")


def test_build_config_disable_sharding_blocks_auto_inject(monkeypatch) -> None:
    fake_meta = {
        "sharded": True,
        "shard_sets": [1, 2, 3, 4, 5],
        "total_bytes": 269 * 1024**3,
    }
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: fake_meta)
    content = blast._build_config_content(
        job_id="job-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/q.fa",
        options={
            "machine_type": "Standard_E16s_v5",
            "num_nodes": 5,
            "disable_sharding": True,
        },
    )
    cfg = _parse_ini(content)
    assert not cfg.has_option("blast", "db-partitions")


def test_upload_split_query_files_returns_state_safe_metadata(monkeypatch) -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n>q2\nCCCC\n")
    plan = build_query_split_execution_plan(
        parent_job_id="job-123",
        metadata=metadata,
        query_effective_search_spaces_value=[225, 300],
        base_options={"outfmt": 6},
    )
    uploads: list[tuple[str, str]] = []

    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_upload_group_fasta(
        _credential: object,
        account_name: str,
        query_blob_path: str,
        group_fasta: str,
    ) -> str:
        assert account_name == "elbstg01"
        uploads.append((query_blob_path, group_fasta))
        return f"https://elbstg01.blob.core.windows.net/queries/{query_blob_path}"

    monkeypatch.setattr("api.services.storage_data.upload_group_fasta", fake_upload_group_fasta)
    monkeypatch.setattr(
        "api.services.storage_data.read_blob_text",
        lambda *_args, **_kwargs: ">verified\nAAAA\n",
    )

    uploaded = blast._upload_split_query_files(storage_account="elbstg01", plan=plan)

    assert uploads == [
        ("split/job-123/qg1/query.fa", ">q1\nAAAA\n"),
        ("split/job-123/qg2/query.fa", ">q2\nCCCC\n"),
    ]
    assert [item["group_id"] for item in uploaded] == ["qg1", "qg2"]
    assert uploaded[0]["query_blob_url"].endswith("/split/job-123/qg1/query.fa")
    assert uploaded[0]["query_fasta_bytes"] == len(b">q1\nAAAA\n")
    assert uploaded[0]["options"]["db_effective_search_space"] == 225
    assert all("query_fasta" not in item for item in uploaded)


def test_upload_split_query_files_verifies_uploaded_blob(monkeypatch) -> None:
    metadata = parse_fasta_metadata(">q1\nAAAA\n")
    plan = build_query_split_execution_plan(
        parent_job_id="job-123",
        metadata=metadata,
        query_effective_search_spaces_value=[225],
        base_options={"outfmt": 6},
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage_data.upload_group_fasta",
        lambda *_args, **_kwargs: "https://elbstg01.blob.core.windows.net/queries/x",
    )
    monkeypatch.setattr("api.services.storage_data.read_blob_text", lambda *_args, **_kwargs: "")

    with pytest.raises(ValueError, match="upload verification failed"):
        blast._upload_split_query_files(storage_account="elbstg01", plan=plan)


def test_build_split_child_submit_plan_generates_group_configs(monkeypatch) -> None:
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *a, **k: None)
    uploaded_groups = [
        {
            "group_id": "qg1",
            "child_job_id": "job-123-qg1",
            "effective_search_space": 225,
            "query_blob_path": "split/job-123/qg1/query.fa",
            "query_file": "queries/split/job-123/qg1/query.fa",
            "query_blob_url": "https://elbstg01.blob.core.windows.net/queries/split/job-123/qg1/query.fa",
            "query_fasta_bytes": 9,
            "options": {
                "sharding_mode": "precise",
                "query_count": 1,
                "db_effective_search_space": 225,
                "query_effective_search_spaces": [225],
                "outfmt": 6,
                "max_target_seqs": 10,
                "machine_type": "Standard_E16s_v5",
                "num_nodes": 5,
                "db_sharded": True,
                "db_partitions": 5,
                "db_partition_prefix": "https://elbstg01.blob.core.windows.net/blast-db/5shards/core_nt_shard_",
                "db_total_letters": 123456,
            },
        }
    ]

    children = blast._build_split_child_submit_plan(
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        uploaded_groups=uploaded_groups,
    )

    assert len(children) == 1
    child = children[0]
    assert child["argv"] == blast._elastic_blast_argv("submit", "job-123-qg1")
    assert "query_fasta" not in child
    cfg = _parse_ini(child["config_content"])
    assert cfg.get("blast", "queries") == (
        "https://elbstg01.blob.core.windows.net/queries/split/job-123/qg1/query.fa"
    )
    assert cfg.get("blast", "db-partitions") == "5"
    assert "-searchsp 225" in cfg.get("blast", "options")
    assert "-max_target_seqs 10" in cfg.get("blast", "options")


def test_build_split_child_submit_plan_rejects_unsafe_option_override() -> None:
    uploaded_groups = [
        {
            "group_id": "qg1",
            "child_job_id": "job-123-qg1",
            "query_file": "queries/split/job-123/qg1/query.fa",
            "options": {"resource_group": "other-rg", "outfmt": 6},
        }
    ]

    with pytest.raises(ValueError, match="unsupported keys"):
        blast._build_split_child_submit_plan(
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            program="blastn",
            database="core_nt",
            uploaded_groups=uploaded_groups,
        )


def test_build_split_child_submit_plan_rejects_incomplete_group() -> None:
    with pytest.raises(ValueError, match="missing"):
        blast._build_split_child_submit_plan(
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            program="blastn",
            database="core_nt",
            uploaded_groups=[{"group_id": "qg1", "options": {"outfmt": 6}}],
        )


def test_dispatch_split_child_submits_creates_state_and_runs_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.state_repo import JobState

    created: list[JobState] = []
    updates: list[tuple[str, dict[str, object]]] = []
    history: list[tuple[str, str, dict[str, object]]] = []

    class FakeRepo:
        def create(self, state: JobState) -> JobState:
            created.append(state)
            return state

        def update(self, job_id: str, **kwargs: object) -> JobState:
            updates.append((job_id, kwargs))
            return JobState(job_id=job_id, type="blast-child", status=str(kwargs.get("status", "")))

        def append_history(self, job_id: str, event: str, payload: dict[str, object]) -> None:
            history.append((job_id, event, payload))

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: FakeRepo())
    terminal_calls: list[dict[str, object]] = []

    def fake_terminal_run(
        *, argv: list[str], stdin: str, stdin_file: str, timeout_seconds: int
    ) -> dict[str, object]:
        terminal_calls.append(
            {
                "argv": argv,
                "stdin": stdin,
                "stdin_file": stdin_file,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"exit_code": 0, "stdout": '{"decision":"accepted"}\n', "stderr": ""}

    child = {
        "group_id": "qg1",
        "child_job_id": "job-123-qg1",
        "query_file": "queries/split/job-123/qg1/query.fa",
        "query_blob_path": "split/job-123/qg1/query.fa",
        "query_blob_url": "https://elbstg01.blob.core.windows.net/queries/split/job-123/qg1/query.fa",
        "query_fasta_bytes": 9,
        "effective_search_space": 225,
        "argv": blast._elastic_blast_argv("submit", "job-123-qg1"),
        "config_content": "[blast]\nqueries=x\n",
        "options": {"outfmt": 6, "db_effective_search_space": 225},
    }

    result = blast._dispatch_split_child_submits(
        parent_job_id="job-123",
        owner_oid="oid-1",
        tenant_id="tenant-1",
        children=[child],
        terminal_run=fake_terminal_run,
    )

    assert result == [
        {
            "child_job_id": "job-123-qg1",
            "group_id": "qg1",
            "status": "running",
            "phase": "submitted",
            "decision": "accepted",
        }
    ]
    assert created[0].parent_job_id == "job-123"
    assert created[0].owner_oid == "oid-1"
    assert created[0].payload is not None
    assert "config_content" not in created[0].payload
    assert "query_fasta" not in created[0].payload
    assert terminal_calls[0]["argv"] == blast._elastic_blast_argv("submit", "job-123-qg1")
    assert terminal_calls[0]["stdin"] == "[blast]\nqueries=x\n"
    assert terminal_calls[0]["stdin_file"] == "elastic-blast.ini"
    assert ("job-123-qg1", {"status": "running", "phase": "submitting"}) in updates
    assert history[-1][1] == "submitted"


def test_dispatch_split_child_submits_records_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.state_repo import JobState

    updates: list[tuple[str, dict[str, object]]] = []

    class FakeRepo:
        def create(self, state: JobState) -> JobState:
            return state

        def update(self, job_id: str, **kwargs: object) -> JobState:
            updates.append((job_id, kwargs))
            return JobState(job_id=job_id, type="blast-child", status=str(kwargs.get("status", "")))

        def append_history(self, _job_id: str, _event: str, _payload: dict[str, object]) -> None:
            return None

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: FakeRepo())

    def fake_terminal_run(
        *, argv: list[str], stdin: str, stdin_file: str, timeout_seconds: int
    ) -> dict[str, object]:
        del argv, stdin, stdin_file, timeout_seconds
        return {"exit_code": 2, "stdout": "", "stderr": "boom"}

    result = blast._dispatch_split_child_submits(
        parent_job_id="job-123",
        owner_oid="oid-1",
        tenant_id="tenant-1",
        children=[
            {
                "group_id": "qg1",
                "child_job_id": "job-123-qg1",
                "query_file": "queries/split/job-123/qg1/query.fa",
                "argv": blast._elastic_blast_argv("submit", "job-123-qg1"),
                "config_content": "[blast]\nqueries=x\n",
                "options": {"outfmt": 6},
            }
        ],
        terminal_run=fake_terminal_run,
    )

    assert result[0]["status"] == "failed"
    assert result[0]["error"] == "boom"
    assert updates[-1] == (
        "job-123-qg1",
        {"status": "failed", "phase": "submit_failed", "error_code": "boom"},
    )


def test_ensure_terminal_azure_cli_login_uses_existing_account() -> None:
    calls: list[list[str]] = []

    def fake_terminal_run(*, argv: list[str], timeout_seconds: int, **_kwargs: object):
        calls.append(argv)
        assert timeout_seconds == 30
        return {"exit_code": 0, "stdout": "operator@example.test\n", "stderr": ""}

    blast._ensure_terminal_azure_cli_login(fake_terminal_run)

    assert calls == [["az", "account", "show", "--query", "user.name", "--output", "tsv"]]


def test_ensure_terminal_azure_cli_login_falls_back_to_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id-123")
    calls: list[list[str]] = []

    def fake_terminal_run(*, argv: list[str], timeout_seconds: int, **_kwargs: object):
        calls.append(argv)
        if len(calls) == 1:
            assert timeout_seconds == 30
            return {"exit_code": 1, "stdout": "", "stderr": "Please run az login"}
        assert timeout_seconds == 120
        return {"exit_code": 0, "stdout": "[]", "stderr": ""}

    blast._ensure_terminal_azure_cli_login(fake_terminal_run)

    assert calls == [
        ["az", "account", "show", "--query", "user.name", "--output", "tsv"],
        ["az", "login", "--identity", "--client-id", "client-id-123"],
    ]


def test_ensure_terminal_azure_cli_login_raises_when_identity_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)

    def fake_terminal_run(*, argv: list[str], **_kwargs: object):
        if argv[:3] == ["az", "account", "show"]:
            return {"exit_code": 1, "stdout": "", "stderr": "Please run az login"}
        return {"exit_code": 1, "stdout": "", "stderr": "identity unavailable"}

    with pytest.raises(blast.TerminalAzureLoginError, match="identity unavailable"):
        blast._ensure_terminal_azure_cli_login(fake_terminal_run)


def test_run_split_parent_submission_dispatches_children_without_raw_fasta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []

    def fake_update_state(
        job_id: str, phase: str, status: str = "running", **details: object
    ) -> None:
        state_updates.append((job_id, phase, {"status": status, **details}))

    monkeypatch.setattr(blast, "_update_state", fake_update_state)

    def fake_upload_split_query_files(
        *, storage_account: str, plan: object
    ) -> list[dict[str, object]]:
        assert storage_account == "elbstg01"
        groups = plan.groups
        assert len(groups) == 2
        return [
            {
                "group_id": group.group_id,
                "child_job_id": group.child_job_id,
                "effective_search_space": group.effective_search_space,
                "query_blob_path": group.query_blob_path,
                "query_file": group.query_file,
                "query_blob_url": (
                    f"https://elbstg01.blob.core.windows.net/queries/{group.query_blob_path}"
                ),
                "query_fasta_bytes": len(group.query_fasta.encode("utf-8")),
                "options": group.options,
            }
            for group in groups
        ]

    monkeypatch.setattr(blast, "_upload_split_query_files", fake_upload_split_query_files)

    def fake_build_split_child_submit_plan(**_kwargs: object) -> list[dict[str, object]]:
        return [
            {"group_id": "qg1", "child_job_id": "job-123-qg1"},
            {"group_id": "qg2", "child_job_id": "job-123-qg2"},
        ]

    monkeypatch.setattr(blast, "_build_split_child_submit_plan", fake_build_split_child_submit_plan)

    def fake_dispatch_split_child_submits(**_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "group_id": "qg1",
                "child_job_id": "job-123-qg1",
                "status": "running",
                "phase": "submitted",
            },
            {
                "group_id": "qg2",
                "child_job_id": "job-123-qg2",
                "status": "running",
                "phase": "submitted",
            },
        ]

    monkeypatch.setattr(blast, "_dispatch_split_child_submits", fake_dispatch_split_child_submits)

    result = blast._run_split_parent_submission(
        parent_job_id="job-123",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_fasta_text=">q1\nAAAA\n>q2\nCCCC\n",
        query_effective_search_spaces=[225, 300],
        options={"outfmt": 6, "max_target_seqs": 10},
        owner_oid="oid-1",
        tenant_id="tenant-1",
    )

    assert result["status"] == "running"
    assert result["phase"] == "split_children_submitted"
    assert result["child_count"] == 2
    assert "query_fasta" not in str(result)
    assert all("AAAA" not in str(update) and "CCCC" not in str(update) for update in state_updates)
    assert state_updates[-1][1] == "split_children_submitted"


def test_run_split_parent_submission_requires_mixed_search_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blast, "_update_state", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="mixed"):
        blast._run_split_parent_submission(
            parent_job_id="job-123",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            program="blastn",
            database="core_nt",
            query_fasta_text=">q1\nAAAA\n>q2\nCCCC\n",
            query_effective_search_spaces=[225, 225],
            options={"outfmt": 6},
            owner_oid="oid-1",
            tenant_id="tenant-1",
        )


def test_run_split_parent_submission_marks_parent_failed_when_child_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr(
        blast,
        "_upload_split_query_files",
        lambda **_kwargs: [
            {
                "group_id": "qg1",
                "child_job_id": "job-123-qg1",
                "query_file": "queries/split/job-123/qg1/query.fa",
                "options": {"outfmt": 6},
            },
            {
                "group_id": "qg2",
                "child_job_id": "job-123-qg2",
                "query_file": "queries/split/job-123/qg2/query.fa",
                "options": {"outfmt": 6},
            },
        ],
    )
    monkeypatch.setattr(
        blast,
        "_build_split_child_submit_plan",
        lambda **_kwargs: [{"group_id": "qg1", "child_job_id": "job-123-qg1"}],
    )
    monkeypatch.setattr(
        blast,
        "_dispatch_split_child_submits",
        lambda **_kwargs: [
            {
                "group_id": "qg1",
                "child_job_id": "job-123-qg1",
                "status": "failed",
                "phase": "submit_failed",
            }
        ],
    )

    result = blast._run_split_parent_submission(
        parent_job_id="job-123",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_fasta_text=">q1\nAAAA\n>q2\nCCCC\n",
        query_effective_search_spaces=[225, 300],
        options={"outfmt": 6},
        owner_oid="oid-1",
        tenant_id="tenant-1",
    )

    assert result["status"] == "failed"
    assert result["phase"] == "split_children_failed"
    assert state_updates[-1][1] == "split_children_failed"
    assert state_updates[-1][2]["failed_child_count"] == 1


def test_query_blob_path_from_query_file_accepts_queries_paths() -> None:
    assert (
        blast._query_blob_path_from_query_file(
            storage_account="elbstg01",
            query_file="queries/original/input.fa",
        )
        == "original/input.fa"
    )
    assert (
        blast._query_blob_path_from_query_file(
            storage_account="elbstg01",
            query_file="https://elbstg01.blob.core.windows.net/queries/original/input.fa",
        )
        == "original/input.fa"
    )
    assert (
        blast._query_blob_path_from_query_file(
            storage_account="elbstg01",
            query_file="az://elbstg01.blob.core.windows.net/queries/original/input.fa",
        )
        == "original/input.fa"
    )


@pytest.mark.parametrize(
    "query_file",
    [
        "../input.fa",
        "/input.fa",
        "queries/split/job-123/qg1/query.fa",
        "folder/split/input.fa",
        "https://other.blob.core.windows.net/queries/input.fa",
        "https://elbstg01.blob.core.windows.net/results/input.fa",
        "queries/input.fa?sig=bad",
    ],
)
def test_query_blob_path_from_query_file_rejects_unsafe_inputs(query_file: str) -> None:
    with pytest.raises(ValueError):
        blast._query_blob_path_from_query_file(
            storage_account="elbstg01",
            query_file=query_file,
        )


def test_run_storage_query_split_parent_submission_reads_blob_and_drops_raw_fasta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    split_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_read_blob_text(
        _credential: object,
        account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        assert account_name == "elbstg01"
        assert container == "queries"
        assert blob_path == "original/input.fa"
        assert max_bytes == blast.QUERY_FASTA_READ_MAX_BYTES + 1
        return ">q1\nAAAA\n>q2\nCCCC\n"

    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)

    def fake_run_split_parent_submission(**kwargs: object) -> dict[str, object]:
        split_calls.append(kwargs)
        return {"job_id": kwargs["parent_job_id"], "status": "running", "phase": "ok"}

    monkeypatch.setattr(blast, "_run_split_parent_submission", fake_run_split_parent_submission)

    result = blast._run_storage_query_split_parent_submission(
        parent_job_id="job-123",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/original/input.fa",
        query_effective_search_spaces=[225, 300],
        options={"outfmt": 6},
        owner_oid="oid-1",
        tenant_id="tenant-1",
    )

    assert result == {"job_id": "job-123", "status": "running", "phase": "ok"}
    assert split_calls[0]["query_fasta_text"] == ">q1\nAAAA\n>q2\nCCCC\n"
    assert state_updates[0][1] == "reading_split_query"
    assert state_updates[0][2]["query_file"] == "original/input.fa"
    assert all("AAAA" not in str(update) and "CCCC" not in str(update) for update in state_updates)
    assert "query_fasta" not in str(result)


def test_run_storage_query_split_parent_submission_rejects_non_fasta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage_data.read_blob_text",
        lambda *_args, **_kwargs: "not fasta",
    )

    with pytest.raises(ValueError, match="FASTA"):
        blast._run_storage_query_split_parent_submission(
            parent_job_id="job-123",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            program="blastn",
            database="core_nt",
            query_file="queries/original/input.fa",
            query_effective_search_spaces=[225, 300],
            options={"outfmt": 6},
            owner_oid="oid-1",
            tenant_id="tenant-1",
        )

    assert state_updates[-1][1] == "split_query_invalid"
    assert state_updates[-1][2]["status"] == "failed"
    assert "not fasta" not in str(state_updates)


def test_run_storage_query_split_parent_submission_rejects_oversized_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blast, "QUERY_FASTA_READ_MAX_BYTES", 8)
    monkeypatch.setattr(blast, "_update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage_data.read_blob_text",
        lambda *_args, **_kwargs: ">q1\nAAAAA\n",
    )

    with pytest.raises(ValueError, match="too large"):
        blast._run_storage_query_split_parent_submission(
            parent_job_id="job-123",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            program="blastn",
            database="core_nt",
            query_file="queries/original/input.fa",
            query_effective_search_spaces=[225, 300],
            options={"outfmt": 6},
            owner_oid="oid-1",
            tenant_id="tenant-1",
        )


def test_run_storage_query_split_parent_submission_reports_missing_query_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blast, "_update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_read_blob_text(*_args: object, **_kwargs: object) -> str:
        raise ResourceNotFoundError("missing")

    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)

    with pytest.raises(ValueError, match="not found"):
        blast._run_storage_query_split_parent_submission(
            parent_job_id="job-123",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            storage_account="elbstg01",
            program="blastn",
            database="core_nt",
            query_file="queries/original/input.fa",
            query_effective_search_spaces=[225, 300],
            options={"outfmt": 6},
            owner_oid="oid-1",
            tenant_id="tenant-1",
        )


def test_requires_split_parent_submission_only_for_mixed_precise_queries() -> None:
    assert blast._requires_split_parent_submission(
        {
            "sharding_mode": "precise",
            "query_count": 2,
            "query_effective_search_spaces": [225, 300],
            "outfmt": 6,
        }
    )
    assert not blast._requires_split_parent_submission(
        {
            "sharding_mode": "precise",
            "query_count": 2,
            "query_effective_search_spaces": [225, 225],
            "outfmt": 6,
        }
    )
    assert not blast._requires_split_parent_submission(
        {
            "sharding_mode": "approximate",
            "query_count": 2,
            "query_effective_search_spaces": [225, 300],
            "outfmt": 6,
        }
    )


def _split_child_state(
    job_id: str,
    status: str,
    *,
    phase: str | None = None,
    error_code: str | None = None,
    group_id: str | None = None,
):
    from api.services.state_repo import JobState

    return JobState(
        job_id=job_id,
        type="blast-child",
        status=status,
        phase=phase,
        parent_job_id="job-123",
        error_code=error_code,
        payload={
            "group_id": group_id or job_id.rsplit("-", 1)[-1],
            "query_file": f"queries/split/job-123/{group_id or 'qg1'}/query.fa",
            "query_fasta_bytes": 9,
            "effective_search_space": 225,
            "config_content": "must-not-leak",
            "query_fasta": "must-not-leak",
        },
    )


class _SplitChildrenRepo:
    def __init__(self, children: list[object]) -> None:
        self.children = children
        self.calls: list[tuple[str, int]] = []

    def list_children(self, parent_job_id: str, limit: int = 100) -> list[object]:
        self.calls.append((parent_job_id, limit))
        return self.children[:limit]


def test_aggregate_split_child_states_marks_merge_ready_without_completing_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "completed", phase="completed", group_id="qg1"),
            _split_child_state("job-123-qg2", "completed", phase="completed", group_id="qg2"),
        ]
    )

    result = blast._aggregate_split_child_states(
        parent_job_id="job-123",
        expected_child_count=2,
        repo=repo,
    )

    assert result["status"] == "running"
    assert result["phase"] == "split_children_merge_ready"
    assert result["ready_for_merge"] is True
    assert result["children_by_status"]["completed"] == 2
    assert all("must-not-leak" not in str(child) for child in result["children"])
    assert state_updates[-1][1] == "split_children_merge_ready"
    assert state_updates[-1][2]["status"] == "running"


def test_aggregate_split_child_states_reports_running_and_missing_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blast, "_update_state", lambda *_args, **_kwargs: None)
    repo = _SplitChildrenRepo(
        [_split_child_state("job-123-qg1", "running", phase="submitted", group_id="qg1")]
    )

    result = blast._aggregate_split_child_states(
        parent_job_id="job-123",
        expected_child_count=2,
        repo=repo,
    )

    assert result["status"] == "running"
    assert result["phase"] == "split_children_aggregating"
    assert result["ready_for_merge"] is False
    assert result["missing_child_count"] == 1


def test_aggregate_split_child_states_marks_parent_failed_on_failed_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "completed", phase="completed", group_id="qg1"),
            _split_child_state(
                "job-123-qg2",
                "failed",
                phase="failed",
                error_code="blast failed",
                group_id="qg2",
            ),
        ]
    )

    result = blast._aggregate_split_child_states(
        parent_job_id="job-123",
        expected_child_count=2,
        repo=repo,
    )

    assert result["status"] == "failed"
    assert result["phase"] == "split_children_failed"
    assert result["failed_children"][0]["job_id"] == "job-123-qg2"
    assert state_updates[-1][1] == "split_children_failed"
    assert state_updates[-1][2]["status"] == "failed"


def test_aggregate_split_child_states_marks_parent_cancelled_on_cancelled_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blast, "_update_state", lambda *_args, **_kwargs: None)
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "completed", phase="completed", group_id="qg1"),
            _split_child_state("job-123-qg2", "cancelled", phase="cancelled", group_id="qg2"),
        ]
    )

    result = blast._aggregate_split_child_states(parent_job_id="job-123", repo=repo)

    assert result["status"] == "cancelled"
    assert result["phase"] == "split_children_cancelled"
    assert result["ready_for_merge"] is False


def test_aggregate_split_child_states_rejects_empty_children() -> None:
    with pytest.raises(ValueError, match="no child"):
        blast._aggregate_split_child_states(parent_job_id="job-123", repo=_SplitChildrenRepo([]))


def test_aggregate_split_child_states_rejects_unknown_status() -> None:
    repo = _SplitChildrenRepo([_split_child_state("job-123-qg1", "mystery")])

    with pytest.raises(ValueError, match="unknown status"):
        blast._aggregate_split_child_states(parent_job_id="job-123", repo=repo)


def test_aggregate_split_child_states_rejects_more_children_than_expected() -> None:
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "running", group_id="qg1"),
            _split_child_state("job-123-qg2", "running", group_id="qg2"),
        ]
    )

    with pytest.raises(ValueError, match="more child jobs"):
        blast._aggregate_split_child_states(
            parent_job_id="job-123",
            expected_child_count=1,
            repo=repo,
        )


def test_aggregate_split_child_states_rejects_possible_truncation() -> None:
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "running", group_id="qg1"),
            _split_child_state("job-123-qg2", "running", group_id="qg2"),
        ]
    )

    with pytest.raises(ValueError, match="truncated"):
        blast._aggregate_split_child_states(
            parent_job_id="job-123",
            child_limit=2,
            repo=repo,
        )


def test_verify_split_child_result_artifacts_detects_missing_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_list_result_blobs(
        _credential: object,
        _account_name: str,
        _container: str,
        prefix: str,
    ) -> list[dict[str, object]]:
        assert prefix == "job-123-qg1/"
        return [{"name": "job-123-qg1/merged_results.out.gz", "size": 42}]

    monkeypatch.setattr("api.services.storage_data.list_result_blobs", fake_list_result_blobs)

    result = blast._verify_split_child_result_artifacts(
        parent_job_id="job-123",
        storage_account="elbstg01",
        children=[_split_child_state("job-123-qg1", "completed", group_id="qg1")],
    )

    assert result["all_artifacts_present"] is False
    assert result["missing_artifacts"] == [
        {
            "child_job_id": "job-123-qg1",
            "group_id": "qg1",
            "missing": ["merge-report.json"],
        }
    ]
    assert result["children"][0]["has_merged_result"] is True
    assert result["children"][0]["has_merge_report"] is False


def test_verify_split_child_result_artifacts_requires_completed_child() -> None:
    with pytest.raises(ValueError, match="not completed"):
        blast._verify_split_child_result_artifacts(
            parent_job_id="job-123",
            storage_account="elbstg01",
            children=[_split_child_state("job-123-qg1", "running", group_id="qg1")],
            credential=object(),
        )


def test_write_split_parent_result_artifacts_concats_child_gzip_and_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [
        _split_child_state("job-123-qg1", "completed", group_id="qg1"),
        _split_child_state("job-123-qg2", "completed", group_id="qg2"),
    ]
    child_outputs = {
        "job-123-qg1/merged_results.out.gz": gzip.compress(b"q1\thit1\n"),
        "job-123-qg2/merged_results.out.gz": gzip.compress(b"q2\thit2\n"),
    }
    child_reports = {
        "job-123-qg1/merge-report.json": {
            "max_target_seqs": 10,
            "queries": 1,
            "total_input_hits": 3,
            "total_output_hits": 1,
            "unsupported_rows": 0,
            "tie_break_count": 0,
            "num_shards": 5,
            "warnings": [],
        },
        "job-123-qg2/merge-report.json": {
            "max_target_seqs": 10,
            "queries": 1,
            "total_input_hits": 4,
            "total_output_hits": 1,
            "unsupported_rows": 1,
            "tie_break_count": 2,
            "num_shards": 5,
            "warnings": ["ties were resolved deterministically"],
        },
    }
    uploads: dict[str, bytes] = {}

    def fake_stream_blob_bytes(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
    ):
        yield child_outputs[blob_path]

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        assert max_bytes == blast.SPLIT_MERGE_REPORT_MAX_BYTES
        return json.dumps(child_reports[blob_path])

    def fake_upload_blob_bytes(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
        data: object,
        *,
        content_type: str,
    ) -> str:
        assert content_type == "application/gzip"
        uploads[blob_path] = b"".join(data)  # type: ignore[arg-type]
        return f"https://example/{blob_path}"

    def fake_upload_blob_text(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
        text: str,
        *,
        content_type: str,
    ) -> str:
        assert content_type == "application/json; charset=utf-8"
        uploads[blob_path] = text.encode("utf-8")
        return f"https://example/{blob_path}"

    monkeypatch.setattr("api.services.storage_data.stream_blob_bytes", fake_stream_blob_bytes)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)
    monkeypatch.setattr("api.services.storage_data.upload_blob_bytes", fake_upload_blob_bytes)
    monkeypatch.setattr("api.services.storage_data.upload_blob_text", fake_upload_blob_text)

    result = blast._write_split_parent_result_artifacts(
        parent_job_id="job-123",
        storage_account="elbstg01",
        children=children,
        artifact_status={"children": [{"child_job_id": "job-123-qg1"}]},
        credential=object(),
    )

    assert gzip.decompress(uploads["job-123/merged_results.out.gz"]) == b"q1\thit1\nq2\thit2\n"
    report = json.loads(uploads["job-123/merge-report.json"].decode("utf-8"))
    assert report["precision_level"] == "split_query_child_finalizer_concat"
    assert report["queries"] == 2
    assert report["total_input_hits"] == 7
    assert report["total_output_hits"] == 2
    assert report["unsupported_rows"] == 1
    assert report["tie_break_count"] == 2
    assert report["num_shards"] == 10
    assert result["paths"]["manifest_path"] == "job-123/split-results-manifest.json"
    assert b"q1\thit1" not in uploads["job-123/merge-report.json"]


def _blast_xml(query_id: str, subject: str) -> bytes:
    return f"""<?xml version=\"1.0\"?>
<BlastOutput>
  <BlastOutput_program>blastn</BlastOutput_program>
  <BlastOutput_version>BLASTN 2.17.0+</BlastOutput_version>
  <BlastOutput_db>child-db</BlastOutput_db>
  <BlastOutput_iterations>
    <Iteration>
      <Iteration_iter-num>1</Iteration_iter-num>
      <Iteration_query-ID>{query_id}</Iteration_query-ID>
      <Iteration_query-def>{query_id}</Iteration_query-def>
      <Iteration_query-len>10</Iteration_query-len>
      <Iteration_hits>
        <Hit>
          <Hit_num>1</Hit_num>
          <Hit_id>{subject}</Hit_id>
          <Hit_def>{subject}</Hit_def>
          <Hit_hsps />
        </Hit>
      </Iteration_hits>
      <Iteration_stat><Statistics /></Iteration_stat>
    </Iteration>
  </BlastOutput_iterations>
</BlastOutput>
""".encode()


def test_write_split_parent_result_artifacts_merges_child_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [
        _split_child_state("job-123-qg1", "completed", group_id="qg1"),
        _split_child_state("job-123-qg2", "completed", group_id="qg2"),
    ]
    child_outputs = {
        "job-123-qg1/merged_results.out.gz": gzip.compress(_blast_xml("q1", "hit1")),
        "job-123-qg2/merged_results.out.gz": gzip.compress(_blast_xml("q2", "hit2")),
    }
    child_reports = {
        "job-123-qg1/merge-report.json": {
            "outfmt": 5,
            "format": "blast_xml",
            "precision_level": "precise_xml",
            "max_target_seqs": 10,
            "queries": 1,
            "total_input_hits": 3,
            "total_output_hits": 1,
            "total_input_hsps": 3,
            "total_output_hsps": 1,
            "malformed_xml_count": 0,
            "unsupported_records": 0,
            "tie_break_count": 0,
            "num_shards": 2,
            "warnings": [],
        },
        "job-123-qg2/merge-report.json": {
            "outfmt": "5",
            "format": "blast_xml",
            "precision_level": "precise_xml",
            "max_target_seqs": 10,
            "queries": 1,
            "total_input_hits": 4,
            "total_output_hits": 1,
            "total_input_hsps": 4,
            "total_output_hsps": 1,
            "malformed_xml_count": 0,
            "unsupported_records": 0,
            "tie_break_count": 1,
            "num_shards": 2,
            "warnings": ["xml warning"],
        },
    }
    uploads: dict[str, bytes] = {}

    def fake_stream_blob_bytes(_credential, _account_name, _container, blob_path):
        yield child_outputs[blob_path]

    def fake_read_blob_text(_credential, _account_name, _container, blob_path, *, max_bytes):
        return json.dumps(child_reports[blob_path])

    def fake_upload_blob_bytes(
        _credential, _account_name, _container, blob_path, data, *, content_type
    ):
        uploads[blob_path] = b"".join(data)
        return f"https://example/{blob_path}"

    def fake_upload_blob_text(
        _credential, _account_name, _container, blob_path, text, *, content_type
    ):
        uploads[blob_path] = text.encode("utf-8")
        return f"https://example/{blob_path}"

    monkeypatch.setattr("api.services.storage_data.stream_blob_bytes", fake_stream_blob_bytes)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)
    monkeypatch.setattr("api.services.storage_data.upload_blob_bytes", fake_upload_blob_bytes)
    monkeypatch.setattr("api.services.storage_data.upload_blob_text", fake_upload_blob_text)

    result = blast._write_split_parent_result_artifacts(
        parent_job_id="job-123",
        storage_account="elbstg01",
        children=children,
        artifact_status={"children": []},
        credential=object(),
    )

    xml_root = ET.fromstring(  # noqa: S314 -- test parses backend-generated fixture XML
        gzip.decompress(uploads["job-123/merged_results.out.gz"])
    )
    assert xml_root.tag == "BlastOutput"
    assert xml_root.find("BlastOutput_program") is not None
    assert xml_root.find("BlastOutput_version") is not None
    assert xml_root.find("BlastOutput_iterations") is not None
    assert [node.text for node in xml_root.findall(".//Iteration_query-ID")] == ["q1", "q2"]
    assert [node.text for node in xml_root.findall(".//Iteration_iter-num")] == ["1", "2"]
    for iteration in xml_root.findall(".//Iteration"):
        assert iteration.find("Iteration_query-ID") is not None
        assert iteration.find("Iteration_hits") is not None
    report = json.loads(uploads["job-123/merge-report.json"].decode("utf-8"))
    assert report["precision_level"] == "split_query_child_finalizer_xml_concat"
    assert report["outfmt"] == 5
    assert report["format"] == "blast_xml"
    assert report["total_input_hsps"] == 7
    assert report["total_output_hsps"] == 2
    assert result["manifest"]["assembly"] == "xml_iteration_concatenation"


def test_finalize_split_parent_results_waits_for_missing_child_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_list_result_blobs(
        _credential: object,
        _account_name: str,
        _container: str,
        prefix: str,
    ) -> list[dict[str, object]]:
        if prefix == "job-123/":
            return []
        if prefix == "job-123-qg1/":
            return [
                {"name": "job-123-qg1/merged_results.out.gz", "size": 42},
                {"name": "job-123-qg1/merge-report.json", "size": 120},
            ]
        if prefix == "job-123-qg2/":
            return [{"name": "job-123-qg2/merged_results.out.gz", "size": 42}]
        return []

    monkeypatch.setattr("api.services.storage_data.list_result_blobs", fake_list_result_blobs)
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "completed", phase="completed", group_id="qg1"),
            _split_child_state("job-123-qg2", "completed", phase="completed", group_id="qg2"),
        ]
    )

    result = blast._finalize_split_parent_results(
        parent_job_id="job-123",
        storage_account="elbstg01",
        expected_child_count=2,
        repo=repo,
    )

    assert result["status"] == "running"
    assert result["phase"] == "split_results_waiting_for_artifacts"
    assert result["artifact_status"]["missing_artifacts"] == [
        {
            "child_job_id": "job-123-qg2",
            "group_id": "qg2",
            "missing": ["merge-report.json"],
        }
    ]
    assert state_updates[-1][1] == "split_results_waiting_for_artifacts"
    assert state_updates[-1][2]["status"] == "running"


def test_finalize_split_parent_results_completes_after_artifacts_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def fake_list_result_blobs(
        _credential: object,
        _account_name: str,
        _container: str,
        prefix: str,
    ) -> list[dict[str, object]]:
        if prefix == "job-123/":
            return []
        return [
            {"name": f"{prefix}merged_results.out.gz", "size": 42},
            {"name": f"{prefix}merge-report.json", "size": 120},
        ]

    monkeypatch.setattr("api.services.storage_data.list_result_blobs", fake_list_result_blobs)
    monkeypatch.setattr(
        blast,
        "_write_split_parent_result_artifacts",
        lambda **_kwargs: {
            "paths": blast._parent_split_result_paths("job-123"),
            "report": {
                "precision_level": "split_query_child_finalizer_concat",
                "queries": 2,
                "total_output_hits": 2,
                "warnings": [],
            },
        },
    )
    repo = _SplitChildrenRepo(
        [
            _split_child_state("job-123-qg1", "completed", phase="completed", group_id="qg1"),
            _split_child_state("job-123-qg2", "completed", phase="completed", group_id="qg2"),
        ]
    )

    result = blast._finalize_split_parent_results(
        parent_job_id="job-123",
        storage_account="elbstg01",
        expected_child_count=2,
        repo=repo,
    )

    assert result["status"] == "completed"
    assert result["phase"] == "completed"
    assert result["outputs"]["merged_result_path"] == "job-123/merged_results.out.gz"
    assert [update[1] for update in state_updates][-2:] == [
        "split_results_merging",
        "completed",
    ]


def test_finalize_split_parent_results_is_idempotent_when_parent_artifacts_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.storage_data.list_result_blobs",
        lambda *_args, **_kwargs: [
            {"name": "job-123/merged_results.out.gz", "size": 42},
            {"name": "job-123/merge-report.json", "size": 120},
            {"name": "job-123/split-results-manifest.json", "size": 200},
        ],
    )
    repo = _SplitChildrenRepo([])

    result = blast._finalize_split_parent_results(
        parent_job_id="job-123",
        storage_account="elbstg01",
        repo=repo,
    )

    assert result["status"] == "completed"
    assert result["already_merged"] is True
    assert state_updates[-1][1] == "completed"
    assert repo.calls == []


def test_cancel_split_parent_cascades_to_children(monkeypatch: pytest.MonkeyPatch) -> None:
    children = [
        _split_child_state("job-123-qg1", "running", group_id="qg1"),
        _split_child_state("job-123-qg2", "running", group_id="qg2"),
    ]
    child_updates: list[tuple[str, dict[str, object]]] = []
    history: list[tuple[str, str, dict[str, object]]] = []
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    cancelled_job_ids: list[str] = []

    class FakeRepo:
        def list_children(self, parent_job_id: str, limit: int = 100) -> list[object]:
            assert parent_job_id == "job-123"
            assert limit == 1000
            return children

        def update(self, job_id: str, **kwargs: object) -> object:
            child_updates.append((job_id, kwargs))
            return object()

        def append_history(self, job_id: str, event: str, payload: dict[str, object]) -> None:
            history.append((job_id, event, payload))

    def fake_cancel_job(
        _credential: object,
        _subscription_id: str,
        _resource_group: str,
        _cluster_name: str,
        *,
        namespace: str,
        job_id: str,
    ) -> dict[str, object]:
        assert namespace == "default"
        cancelled_job_ids.append(job_id)
        return {"status": "cancelled"}

    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr("api.services.monitoring.k8s_cancel_blast_job", fake_cancel_job)
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )

    result = blast.cancel.run(
        job_id="job-123",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
    )

    assert result["status"] == "cancelled"
    assert cancelled_job_ids == ["job-123-qg1", "job-123-qg2"]
    assert child_updates == [
        ("job-123-qg1", {"status": "cancelled", "phase": "cancelled"}),
        ("job-123-qg2", {"status": "cancelled", "phase": "cancelled"}),
    ]
    assert [item[1] for item in history] == ["cancelled_by_parent", "cancelled_by_parent"]
    assert state_updates[-1][1] == "cancelled"
    assert state_updates[-1][2]["child_count"] == 2


def test_check_status_finalizes_split_parent_when_children_merge_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [
        _split_child_state("job-123-qg1", "completed", phase="completed", group_id="qg1"),
        _split_child_state("job-123-qg2", "completed", phase="completed", group_id="qg2"),
    ]
    state_updates: list[tuple[str, str, dict[str, object]]] = []
    finalize_calls: list[dict[str, object]] = []

    class FakeRepo(_SplitChildrenRepo):
        pass

    repo = FakeRepo(children)
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)
    monkeypatch.setattr(
        blast,
        "_update_state",
        lambda job_id, phase, status="running", **details: state_updates.append(
            (job_id, phase, {"status": status, **details})
        ),
    )

    def fake_finalize(**kwargs: object) -> dict[str, object]:
        finalize_calls.append(kwargs)
        return {"parent_job_id": "job-123", "status": "completed", "phase": "completed"}

    monkeypatch.setattr(blast, "_finalize_split_parent_results", fake_finalize)

    result = blast.check_status.run(
        job_id="job-123",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
    )

    assert result == {"parent_job_id": "job-123", "status": "completed", "phase": "completed"}
    assert finalize_calls[0]["parent_job_id"] == "job-123"
    assert finalize_calls[0]["storage_account"] == "elbstg01"
    assert state_updates[-1][1] == "split_children_merge_ready"


def test_merge_progress_payload_keeps_submit_context_and_live_output() -> None:
    payload = blast._merge_progress_payload(
        {"program": "blastn", "db": "core_nt"},
        phase="submitting",
        status="running",
        error_code="",
        details={"last_output": "kubectl logs line", "ignored": "large-detail"},
    )

    assert payload["program"] == "blastn"
    assert payload["db"] == "core_nt"
    assert payload["_progress"]["phase"] == "submitting"
    assert payload["_progress"]["steps"]["submitting"]["last_output"] == "kubectl logs line"
    assert "ignored" not in payload["_progress"]["steps"]["submitting"]


def test_merge_progress_payload_keeps_completed_submit_output() -> None:
    payload = blast._merge_progress_payload(
        {"program": "blastn", "db": "core_nt"},
        phase="submitting",
        status="completed",
        error_code="",
        details={"output": "final elastic-blast log", "log_line_count": 12},
    )

    step = payload["_progress"]["steps"]["submitting"]
    assert step["status"] == "completed"
    assert step["success"] is True
    assert step["output"] == "final elastic-blast log"
    assert step["log_line_count"] == 12


def test_merge_progress_payload_completes_previous_running_steps() -> None:
    payload = blast._merge_progress_payload(
        {
            "_progress": {
                "phase": "submitting",
                "status": "running",
                "steps": {
                    "preparing": {"phase": "preparing", "status": "running"},
                    "warming_up": {"phase": "warmup_ready", "status": "running"},
                    "configuring": {"phase": "configuring", "status": "running"},
                    "submitting": {
                        "phase": "submitting",
                        "status": "running",
                        "last_output": "submit log",
                    },
                    "failed_step": {"phase": "failed_step", "status": "failed"},
                },
            }
        },
        phase="completed",
        status="completed",
        error_code="",
        details={"k8s": {"succeeded": 10}},
    )

    steps = payload["_progress"]["steps"]
    assert payload["_progress"]["status"] == "completed"
    for key in ("preparing", "warming_up", "configuring", "submitting"):
        assert steps[key]["status"] == "completed"
        assert steps[key]["success"] is True
    assert steps["submitting"]["last_output"] == "submit log"
    assert steps["failed_step"]["status"] == "failed"
    assert steps["completed"]["k8s"] == {"succeeded": 10}


def test_merge_progress_payload_completes_steps_when_phase_advances() -> None:
    payload = blast._merge_progress_payload(
        {
            "_progress": {
                "phase": "warmup_ready",
                "status": "running",
                "steps": {
                    "preparing": {"phase": "preparing", "status": "running"},
                    "warming_up": {"phase": "warmup_ready", "status": "running"},
                },
            }
        },
        phase="configuring",
        status="running",
        error_code="",
        details={"config_blob_path": "queries/job-123/elastic-blast.ini"},
    )

    steps = payload["_progress"]["steps"]
    assert payload["_progress"]["phase"] == "configuring"
    assert steps["preparing"]["status"] == "completed"
    assert steps["warming_up"]["status"] == "completed"
    assert steps["preparing"]["success"] is True
    assert steps["warming_up"]["success"] is True
    assert steps["configuring"]["status"] == "running"
    assert steps["configuring"]["config_blob_path"] == "queries/job-123/elastic-blast.ini"


def test_merge_progress_payload_marks_warmup_ready_step_completed() -> None:
    payload = blast._merge_progress_payload(
        {"_progress": {"phase": "preparing", "status": "running", "steps": {}}},
        phase="warmup_ready",
        status="running",
        error_code="",
        details={},
    )

    progress = payload["_progress"]
    step = progress["steps"]["warming_up"]
    assert progress["phase"] == "warmup_ready"
    assert progress["status"] == "running"
    assert step["status"] == "completed"
    assert step["success"] is True


def test_merge_progress_payload_tracks_staging_db_before_submit() -> None:
    payload = blast._merge_progress_payload(
        {
            "_progress": {
                "phase": "configuring",
                "status": "running",
                "steps": {
                    "configuring": {"phase": "configuring", "status": "running"},
                },
            }
        },
        phase="staging_db",
        status="running",
        error_code="",
        details={"last_output": "init-ssd progress"},
    )

    steps = payload["_progress"]["steps"]
    assert payload["_progress"]["phase"] == "staging_db"
    assert steps["configuring"]["status"] == "completed"
    assert steps["staging_db"]["status"] == "running"
    assert steps["staging_db"]["last_output"] == "init-ssd progress"


def test_split_parent_storage_submit_to_finalize_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.state_repo import JobState

    states: dict[str, JobState] = {
        "job-123": JobState(
            job_id="job-123",
            type="blast",
            status="queued",
            phase="queued",
            owner_oid="oid-1",
            tenant_id="tenant-1",
        )
    }
    history: list[tuple[str, str, dict[str, object]]] = []
    uploaded_queries: list[str] = []
    uploaded_results: dict[str, bytes] = {}

    class FakeRepo:
        def create(self, state: JobState) -> JobState:
            states[state.job_id] = state
            return state

        def get(self, job_id: str) -> JobState | None:
            return states.get(job_id)

        def update(self, job_id: str, **kwargs: object) -> JobState:
            state = states[job_id]
            for key, value in kwargs.items():
                if key == "status":
                    state.status = str(value)
                elif key == "phase":
                    state.phase = str(value)
                elif key == "error_code":
                    state.error_code = str(value)
            return state

        def append_history(self, job_id: str, event: str, payload: dict[str, object]) -> None:
            history.append((job_id, event, payload))

        def list_children(self, parent_job_id: str, limit: int = 100) -> list[JobState]:
            children = [state for state in states.values() if state.parent_job_id == parent_job_id]
            return children[:limit]

    query_fasta = ">q1\nAAAA\n>q2\nCCCC\n"
    child_report = {
        "max_target_seqs": 10,
        "queries": 1,
        "total_input_hits": 1,
        "total_output_hits": 1,
        "unsupported_rows": 0,
        "tie_break_count": 0,
        "num_shards": 5,
        "warnings": [],
    }

    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: FakeRepo())
    monkeypatch.setattr(blast, "resolve_db_metadata", lambda *_args, **_kwargs: None)

    def fake_upload_group_fasta(
        _credential: object,
        _account_name: str,
        query_blob_path: str,
        _group_fasta: str,
    ) -> str:
        uploaded_queries.append(query_blob_path)
        return f"https://elbstg01.blob.core.windows.net/queries/{query_blob_path}"

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        del max_bytes
        if container == "queries" and blob_path == "original/input.fa":
            return query_fasta
        if container == "queries":
            return ">verified\nAAAA\n"
        if container == "results" and blob_path.endswith("merge-report.json"):
            return json.dumps(child_report)
        raise AssertionError(f"unexpected blob read: {container}/{blob_path}")

    def fake_terminal_run(
        *, argv: list[str], stdin: str, stdin_file: str, timeout_seconds: int
    ) -> dict[str, object]:
        assert argv[0] == "elastic-blast"
        assert "AAAA" not in stdin and "CCCC" not in stdin
        assert stdin_file == "elastic-blast.ini"
        assert timeout_seconds == 600
        return {"exit_code": 0, "stdout": '{"decision":"accepted"}\n', "stderr": ""}

    def fake_list_result_blobs(
        _credential: object,
        _account_name: str,
        _container: str,
        prefix: str,
    ) -> list[dict[str, object]]:
        if prefix == "job-123/":
            return []
        return [
            {"name": f"{prefix}merged_results.out.gz", "size": 42},
            {"name": f"{prefix}merge-report.json", "size": 120},
        ]

    def fake_stream_blob_bytes(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
    ):
        yield gzip.compress(f"{blob_path}\n".encode())

    def fake_upload_blob_bytes(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
        data: object,
        *,
        content_type: str,
    ) -> str:
        assert content_type == "application/gzip"
        uploaded_results[blob_path] = b"".join(data)  # type: ignore[arg-type]
        return f"https://example/{blob_path}"

    def fake_upload_blob_text(
        _credential: object,
        _account_name: str,
        _container: str,
        blob_path: str,
        text: str,
        *,
        content_type: str,
    ) -> str:
        assert content_type == "application/json; charset=utf-8"
        uploaded_results[blob_path] = text.encode("utf-8")
        return f"https://example/{blob_path}"

    monkeypatch.setattr("api.services.storage_data.upload_group_fasta", fake_upload_group_fasta)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)
    monkeypatch.setattr("api.services.storage_data.list_result_blobs", fake_list_result_blobs)
    monkeypatch.setattr("api.services.storage_data.stream_blob_bytes", fake_stream_blob_bytes)
    monkeypatch.setattr("api.services.storage_data.upload_blob_bytes", fake_upload_blob_bytes)
    monkeypatch.setattr("api.services.storage_data.upload_blob_text", fake_upload_blob_text)

    split_result = blast._run_storage_query_split_parent_submission(
        parent_job_id="job-123",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        storage_account="elbstg01",
        program="blastn",
        database="core_nt",
        query_file="queries/original/input.fa",
        query_effective_search_spaces=[225, 300],
        options={"sharding_mode": "precise", "outfmt": 6, "max_target_seqs": 10},
        owner_oid="oid-1",
        tenant_id="tenant-1",
        terminal_run=fake_terminal_run,
    )
    for state in states.values():
        if state.parent_job_id == "job-123":
            state.status = "completed"
            state.phase = "completed"

    final_result = blast._finalize_split_parent_results(
        parent_job_id="job-123",
        storage_account="elbstg01",
        expected_child_count=2,
        repo=FakeRepo(),
        credential=object(),
    )

    assert split_result["phase"] == "split_children_submitted"
    assert uploaded_queries == ["split/job-123/qg1/query.fa", "split/job-123/qg2/query.fa"]
    assert final_result["status"] == "completed"
    assert "job-123/merged_results.out.gz" in uploaded_results
    assert "job-123/merge-report.json" in uploaded_results
    assert "job-123/split-results-manifest.json" in uploaded_results
    assert gzip.decompress(uploaded_results["job-123/merged_results.out.gz"]).count(b".out.gz") == 2
    assert "AAAA" not in str(history)
    assert "CCCC" not in str(history)


# ---------------------------------------------------------------------------
# reconcile_stale_jobs — Celery resilience tests
# ---------------------------------------------------------------------------


class _StaleRow:
    """Minimal jobstate row stand-in for reconcile_stale_jobs tests."""

    def __init__(
        self,
        *,
        job_id: str,
        status: str = "running",
        phase: str = "running",
        task_id: str = "",
        updated_at: str = "2026-05-20T00:00:00+00:00",
        created_at: str = "2026-05-20T00:00:00+00:00",
        payload: dict[str, object] | None = None,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
    ) -> None:
        self.job_id = job_id
        self.status = status
        self.phase = phase
        self.task_id = task_id
        self.updated_at = updated_at
        self.created_at = created_at
        self.payload = payload
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.cluster_name = cluster_name


class _FakeReconcileRepo:
    def __init__(self, active: list[_StaleRow]) -> None:
        self._active = active
        self.updates: list[tuple[str, dict[str, object]]] = []

    def list_active(self, *, job_type: str = "blast", limit: int = 500) -> list[_StaleRow]:
        return list(self._active)

    def update(self, job_id: str, **kwargs: object) -> None:
        self.updates.append((job_id, kwargs))


def _install_repo(monkeypatch: pytest.MonkeyPatch, repo: _FakeReconcileRepo) -> None:
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)


def test_reconcile_celery_failure_marks_row_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Celery FAILURE → row becomes failed with the snippet as error_code."""
    repo = _FakeReconcileRepo([_StaleRow(job_id="j1", task_id="task-1")])
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "FAILURE"
            self.result = RuntimeError("worker exploded")

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)

    summary = blast.reconcile_stale_jobs.run()

    assert summary["failed"] == 1
    assert summary["completed"] == 0
    assert repo.updates and repo.updates[0][0] == "j1"
    assert repo.updates[0][1]["status"] == "failed"
    assert repo.updates[0][1]["phase"] == "failed"


def test_reconcile_celery_success_marks_row_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Celery SUCCESS → row becomes completed when status was stale."""
    repo = _FakeReconcileRepo([_StaleRow(job_id="j2", task_id="task-2", status="running")])
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "SUCCESS"
            self.result = {"ok": True}

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)

    summary = blast.reconcile_stale_jobs.run()

    assert summary["completed"] == 1
    assert repo.updates[0][1]["status"] == "completed"


def test_reconcile_submit_success_keeps_running_row_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeReconcileRepo([_StaleRow(job_id="j2", task_id="task-2", status="running")])
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "SUCCESS"
            self.result = {"status": "running", "phase": "submitted"}

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)

    summary = blast.reconcile_stale_jobs.run()

    assert summary["completed"] == 0
    assert summary["untouched"] == 1
    assert repo.updates[0][1] == {"status": "running", "phase": "submitted"}


def test_reconcile_submit_completed_waits_for_result_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeReconcileRepo(
        [
            _StaleRow(
                job_id="j2",
                task_id="task-2",
                status="running",
                payload={"storage_account": "stelb"},
            )
        ]
    )
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "SUCCESS"
            self.result = {"status": "completed", "phase": "completed"}

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)
    monkeypatch.setattr(blast, "_has_parseable_result_artifact", lambda *_args: False)

    summary = blast.reconcile_stale_jobs.run()

    assert summary["completed"] == 0
    assert summary["untouched"] == 1
    assert repo.updates[0][1] == {"status": "running", "phase": "results_pending"}


def test_reconcile_skips_recently_updated_unknown_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A young row with no Celery record MUST NOT be killed off."""
    from datetime import datetime

    recent = datetime.now(UTC).isoformat(timespec="seconds")
    repo = _FakeReconcileRepo(
        [_StaleRow(job_id="j3", task_id="", updated_at=recent, created_at=recent)]
    )
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "PENDING"
            self.result = None

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)

    summary = blast.reconcile_stale_jobs.run()

    assert summary["untouched"] >= 1
    assert summary["worker_lost"] == 0
    assert repo.updates == []


def test_reconcile_marks_old_quiet_row_worker_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row that hasn't moved past the stale threshold MUST be marked lost."""
    repo = _FakeReconcileRepo(
        [
            _StaleRow(
                job_id="j4",
                task_id="task-4",
                updated_at="2025-01-01T00:00:00+00:00",
                created_at="2025-01-01T00:00:00+00:00",
            )
        ]
    )
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "PENDING"
            self.result = None

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)

    summary = blast.reconcile_stale_jobs.run(stale_threshold_seconds=60)

    assert summary["worker_lost"] == 1
    assert repo.updates[0][1]["error_code"] == "worker_lost"
    assert repo.updates[0][1]["phase"] == "worker_lost"


def test_reconcile_logs_external_refresh_http_detail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recent = "2026-05-20T00:00:00+00:00"
    repo = _FakeReconcileRepo(
        [
            _StaleRow(
                job_id="j5",
                task_id="task-5",
                updated_at=recent,
                created_at=recent,
                payload={
                    "subscription_id": "sub-1",
                    "resource_group": "rg-elb",
                    "cluster_name": "elb-cluster",
                    "elastic_blast_job_id": "job-j5",
                },
            )
        ]
    )
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "PENDING"
            self.result = None

    def fail_get_job(job_id: str, **_kwargs: object) -> dict[str, object]:
        assert job_id == "job-j5"
        raise HTTPException(
            400,
            detail={
                "code": "openapi_http_400",
                "message": "job id is not known yet",
                "upstream_status": 400,
            },
        )

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)
    monkeypatch.setattr(
        "api.routes._blast_shared._openapi_client_kwargs_from_cluster",
        lambda *_args: {"base_url": "http://openapi.test"},
    )
    monkeypatch.setattr("api.services.external_blast.get_job", fail_get_job)
    caplog.set_level("WARNING", logger="api.tasks.blast")

    summary = blast.reconcile_stale_jobs.run(stale_threshold_seconds=99999999)

    assert summary["untouched"] == 1
    assert "external refresh failed job_id=j5" in caplog.text
    assert "status_code=400" in caplog.text
    assert "job id is not known yet" in caplog.text


def test_reconcile_skips_external_refresh_without_elastic_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recent = "2026-05-20T00:00:00+00:00"
    repo = _FakeReconcileRepo(
        [
            _StaleRow(
                job_id="dashboard-uuid",
                task_id="task-dashboard",
                updated_at=recent,
                created_at=recent,
                payload={
                    "subscription_id": "sub-1",
                    "resource_group": "rg-elb",
                    "cluster_name": "elb-cluster",
                },
            )
        ]
    )
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "PENDING"
            self.result = None

    def fail_get_job(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("external refresh should require an ElasticBLAST job id")

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)
    monkeypatch.setattr(
        "api.routes._blast_shared._openapi_client_kwargs_from_cluster",
        lambda *_args: {"base_url": "http://openapi.test"},
    )
    monkeypatch.setattr("api.services.external_blast.get_job", fail_get_job)

    summary = blast.reconcile_stale_jobs.run(stale_threshold_seconds=99999999)

    assert summary["untouched"] == 1
    assert summary["external_refreshed"] == 0
    assert repo.updates == []


def test_reconcile_does_not_treat_dashboard_job_id_as_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recent = "2026-05-20T00:00:00+00:00"
    repo = _FakeReconcileRepo(
        [
            _StaleRow(
                job_id="job-dashboard-only",
                task_id="task-dashboard",
                updated_at=recent,
                created_at=recent,
                payload={
                    "subscription_id": "sub-1",
                    "resource_group": "rg-elb",
                    "cluster_name": "elb-cluster",
                },
            )
        ]
    )
    _install_repo(monkeypatch, repo)

    class FakeAsync:
        def __init__(self, *_args, **_kwargs) -> None:
            self.status = "PENDING"
            self.result = None

    def fail_get_job(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("dashboard job ids must not be external ids")

    monkeypatch.setattr("celery.result.AsyncResult", FakeAsync)
    monkeypatch.setattr(
        "api.routes._blast_shared._openapi_client_kwargs_from_cluster",
        lambda *_args: {"base_url": "http://openapi.test"},
    )
    monkeypatch.setattr("api.services.external_blast.get_job", fail_get_job)

    summary = blast.reconcile_stale_jobs.run(stale_threshold_seconds=99999999)

    assert summary["untouched"] == 1
    assert summary["external_refreshed"] == 0
    assert repo.updates == []


def test_update_state_skips_identical_empty_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExistingState:
        def __init__(self) -> None:
            self.status = "running"
            self.phase = "submitting"
            self.error_code = ""
            self.payload: dict[str, object] = {}

    class FakeRepo:
        def __init__(self) -> None:
            self.updated = False
            self.history_appended = False

        def get(self, _job_id: str) -> ExistingState:
            return ExistingState()

        def update(self, *_args: object, **_kwargs: object) -> None:
            self.updated = True

        def append_history(self, *_args: object, **_kwargs: object) -> None:
            self.history_appended = True

    repo = FakeRepo()
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)

    blast._update_state("job-1", "submitting")

    assert repo.updated is False
    assert repo.history_appended is False


def test_update_state_duplicate_terminal_checkpoint_still_finalizes_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExistingState:
        def __init__(self) -> None:
            self.status = "completed"
            self.phase = "completed"
            self.error_code = ""
            self.payload: dict[str, object] = {}

    class FakeRepo:
        def get(self, _job_id: str) -> ExistingState:
            return ExistingState()

        def update(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("duplicate terminal checkpoint should not rewrite state")

        def append_history(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("duplicate terminal checkpoint should not append history")

    enqueued: list[tuple[str, str, str]] = []
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)
    monkeypatch.setattr(
        blast,
        "_enqueue_artifact_finalizer",
        lambda job_id, phase, status: enqueued.append((job_id, phase, status)),
    )

    blast._update_state("job-1", "completed", status="completed")

    assert enqueued == [("job-1", "completed", "completed")]


# Local import to keep the file's existing imports section unchanged.
from datetime import UTC  # noqa: E402
