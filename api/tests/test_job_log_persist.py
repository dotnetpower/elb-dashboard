"""Tests for `api.services.job_logs.persist`.

Responsibility: Verify pod-log persistence at finalize is idempotent, groups
by phase, writes chunks, and updates ``payload._progress.steps``.
Edit boundaries: Use fakes — no live Azure / Kubernetes credentials.
Key entry points: `test_persist_writes_chunks_and_last_output`,
`test_persist_skips_when_inputs_missing`,
`test_persist_skips_when_no_targets`,
`test_persist_does_not_clobber_longer_existing_last_output`
Risky contracts: Pod logs must be sanitised; truncation must keep head + tail.
Validation: `uv run pytest -q api/tests/test_job_log_persist.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from api.services.job_logs import k8s, persist


class _FakeRepo:
    def __init__(self, state: Any) -> None:
        self._state = state
        self.updates: list[dict[str, Any]] = []

    def get(self, _job_id: str) -> Any:
        return self._state

    def update(self, job_id: str, **kwargs: Any) -> None:
        self.updates.append({"job_id": job_id, **kwargs})
        if "payload" in kwargs and isinstance(kwargs["payload"], dict):
            self._state.payload = dict(kwargs["payload"])


def _make_state(**payload_overrides: Any) -> SimpleNamespace:
    payload: dict[str, Any] = {
        "elastic_blast_job_id": "job-aaaaaaaa11111111",
        "subscription_id": "sub-1",
        "resource_group": "rg-elb",
        "cluster_name": "elb-cluster",
        "_progress": {"steps": {}},
    }
    payload.update(payload_overrides)
    return SimpleNamespace(
        job_id="dash-job",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        payload=payload,
    )


def test_persist_writes_chunks_and_last_output(monkeypatch) -> None:
    targets = [
        k8s.K8sLogTarget(
            namespace="default",
            pod_name="blastn-batch-s00-job-000-11111111-abcde",
            container_name="blast",
            phase="running",
        ),
        k8s.K8sLogTarget(
            namespace="default",
            pod_name="blastn-batch-s00-job-000-11111111-abcde",
            container_name="results-export",
            phase="running",
        ),
        k8s.K8sLogTarget(
            namespace="default",
            pod_name="init-ssd-11111111-0-abcde",
            container_name="get-blastdb",
            phase="staging_db",
        ),
    ]
    tails = {
        ("blastn-batch-s00-job-000-11111111-abcde", "blast"): [
            "BLAST RUNTIME: 12.3 seconds",
            "RUN END",
        ],
        ("blastn-batch-s00-job-000-11111111-abcde", "results-export"): [
            "INFO: azcopy upload completed",
        ],
        ("init-ssd-11111111-0-abcde", "get-blastdb"): [
            "fetched core_nt shard 1",
        ],
    }

    monkeypatch.setattr(
        persist,
        "discover_k8s_log_targets",
        lambda *args, **kwargs: targets,
    )

    def fake_tail(*_args: Any, target: k8s.K8sLogTarget, tail_lines: int = 200) -> list[str]:
        assert tail_lines == 200
        return tails[(target.pod_name, target.container_name)]

    monkeypatch.setattr(
        persist,
        "fetch_k8s_pod_log_tail",
        lambda credential, sub, rg, cluster, target, *, tail_lines=200: fake_tail(
            target=target, tail_lines=tail_lines
        ),
    )

    chunks: list[tuple[str, str, int, list[dict[str, Any]]]] = []

    def fake_write_chunk(job_id: str, step: str, seq: int, events: list[dict[str, Any]]) -> None:
        chunks.append((job_id, step, seq, list(events)))

    state = _make_state()
    repo = _FakeRepo(state)

    monkeypatch.setattr(
        "api.services.job_artifacts.write_execution_log_chunk",
        fake_write_chunk,
    )
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        lambda: repo,
    )

    result = persist.persist_completed_job_pod_logs(object(), state)

    assert result == {"running": 3, "staging_db": 1}
    chunk_steps = {(step, seq) for _job, step, seq, _events in chunks}
    assert ("running", 0) in chunk_steps
    assert ("staging_db", 0) in chunk_steps

    progress_steps = state.payload["_progress"]["steps"]
    running_text = progress_steps["running"]["last_output"]
    assert "BLAST RUNTIME: 12.3 seconds" in running_text
    assert "azcopy upload completed" in running_text
    assert progress_steps["running"]["pod_log_persisted"] is True
    staging_text = progress_steps["staging_db"]["last_output"]
    assert "fetched core_nt shard 1" in staging_text


def test_persist_skips_when_inputs_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        persist,
        "discover_k8s_log_targets",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    state = SimpleNamespace(
        job_id="dash-job",
        subscription_id="",
        resource_group="",
        cluster_name="",
        payload={},
    )
    assert persist.persist_completed_job_pod_logs(object(), state) == {}


def test_persist_skips_when_no_targets(monkeypatch) -> None:
    monkeypatch.setattr(persist, "discover_k8s_log_targets", lambda *args, **kwargs: [])
    state = _make_state()
    assert persist.persist_completed_job_pod_logs(object(), state) == {}


def test_persist_does_not_clobber_longer_existing_last_output(monkeypatch) -> None:
    long_existing = "x" * 1000
    state = _make_state(
        _progress={"steps": {"running": {"last_output": long_existing}}},
    )
    targets = [
        k8s.K8sLogTarget(
            namespace="default",
            pod_name="blastn-batch-s00-job-000-11111111-abcde",
            container_name="blast",
            phase="running",
        ),
    ]
    monkeypatch.setattr(persist, "discover_k8s_log_targets", lambda *args, **kwargs: targets)
    monkeypatch.setattr(
        persist,
        "fetch_k8s_pod_log_tail",
        lambda *args, **kwargs: ["short"],
    )
    monkeypatch.setattr(
        "api.services.job_artifacts.write_execution_log_chunk",
        lambda *args, **kwargs: None,
    )
    repo = _FakeRepo(state)
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", lambda: repo)

    persist.persist_completed_job_pod_logs(object(), state)
    # The longer existing tail should be preserved.
    assert state.payload["_progress"]["steps"]["running"]["last_output"] == long_existing
    assert state.payload["_progress"]["steps"]["running"]["pod_log_persisted"] is True
