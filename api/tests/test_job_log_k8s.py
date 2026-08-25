"""Tests for Job Log Kubernetes behavior.

Responsibility: Tests for Job Log Kubernetes behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_discover_k8s_log_targets_filters_to_job_and_maps_phases`,
`test_stream_k8s_log_lines_uses_following_pod_log_api`,
`test_resolve_elastic_blast_job_id_*`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_job_log_k8s.py`.
"""

from __future__ import annotations

import threading

from api.services.job_logs import k8s


def test_discover_k8s_log_targets_filters_to_job_and_maps_phases(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "init-ssd-e2fc8081-0-abcde",
                            "labels": {"elb-job-id": "job-000000000000000000000000e2fc8081"},
                            "ownerReferences": [{"kind": "Job", "name": "init-ssd-e2fc8081-0"}],
                        },
                        "spec": {
                            "containers": [
                                {"name": "get-blastdb"},
                                {"name": "import-query-batches"},
                            ]
                        },
                    },
                    {
                        "metadata": {
                            "name": "blastn-batch-s00-job-000-e2fc8081-abcde",
                            "labels": {},
                            "ownerReferences": [
                                {"kind": "Job", "name": "blastn-batch-s00-job-000-e2fc8081"}
                            ],
                        },
                        "spec": {
                            "containers": [
                                {
                                    "name": "blast",
                                    "env": [
                                        {
                                            "name": "BLAST_ELB_JOB_ID",
                                            "value": "job-000000000000000000000000e2fc8081",
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                    {
                        "metadata": {"name": "unrelated", "labels": {}},
                        "spec": {"containers": [{"name": "main"}]},
                    },
                ]
            }

    class FakeSession:
        def get(self, url, *, timeout):
            assert url.endswith("/api/v1/namespaces/default/pods")
            assert timeout == 10
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        k8s,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (FakeSession(), "https://k8s"),
    )

    targets = k8s.discover_k8s_log_targets(
        object(),
        "sub-1",
        "rg-elb",
        "elb-cluster",
        namespace="default",
        job_id="dashboard-job",
        elastic_job_id="job-000000000000000000000000e2fc8081",
    )

    assert [(target.pod_name, target.container_name, target.phase) for target in targets] == [
        ("blastn-batch-s00-job-000-e2fc8081-abcde", "blast", "running"),
        ("init-ssd-e2fc8081-0-abcde", "get-blastdb", "staging_db"),
        ("init-ssd-e2fc8081-0-abcde", "import-query-batches", "staging_db"),
    ]


def test_discover_k8s_log_targets_includes_init_containers(monkeypatch) -> None:
    """A failed BLAST search pod's init container (`import-query-batches`) must
    be discovered — it runs before the main `blast` container and is where a
    query-staging failure is logged."""

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "blastn-batch-s00-job-000-e2fc8081-abcde",
                            "labels": {},
                            "ownerReferences": [
                                {"kind": "Job", "name": "blastn-batch-s00-job-000-e2fc8081"}
                            ],
                        },
                        "spec": {
                            "initContainers": [{"name": "import-query-batches"}],
                            "containers": [
                                {
                                    "name": "blast",
                                    "env": [
                                        {
                                            "name": "BLAST_ELB_JOB_ID",
                                            "value": "job-000000000000000000000000e2fc8081",
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ]
            }

    class FakeSession:
        def get(self, url, *, timeout):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        k8s,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (FakeSession(), "https://k8s"),
    )

    targets = k8s.discover_k8s_log_targets(
        object(),
        "sub-1",
        "rg-elb",
        "elb-cluster",
        namespace="default",
        job_id="dashboard-job",
        elastic_job_id="job-000000000000000000000000e2fc8081",
    )

    container_names = {target.container_name for target in targets}
    assert "import-query-batches" in container_names
    assert "blast" in container_names


def test_stream_k8s_log_lines_uses_following_pod_log_api(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self):
            pass

        def iter_lines(self, *, decode_unicode):
            assert decode_unicode is True
            yield "2026-05-20T00:00:00Z first"
            yield "second"

    class FakeSession:
        def get(self, url, *, params, stream, timeout):
            assert url.endswith("/api/v1/namespaces/default/pods/pod-1/log")
            assert params["container"] == "main"
            assert params["follow"] == "true"
            assert params["timestamps"] == "true"
            assert params["tailLines"] == 25
            assert stream is True
            assert timeout == (10, 65)
            return FakeResponse()

        def close(self):
            pass

    target = k8s.K8sLogTarget(
        namespace="default",
        pod_name="pod-1",
        container_name="main",
        phase="running",
    )
    monkeypatch.setattr(
        k8s,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (FakeSession(), "https://k8s"),
    )

    lines = list(
        k8s.stream_k8s_log_lines(
            object(),
            "sub-1",
            "rg-elb",
            "elb-cluster",
            target,
            tail_lines=25,
            stop_event=threading.Event(),
        )
    )

    assert lines == ["2026-05-20T00:00:00Z first", "second"]


def test_resolve_elastic_blast_job_id_prefers_top_level() -> None:
    assert (
        k8s.resolve_elastic_blast_job_id(
            {
                "elastic_blast_job_id": "job-00000000000000000000000000aaa111",
                "_progress": {
                    "steps": {
                        "running": {"k8s": {"job_id": "job-00000000000000000000000000bbb222"}}
                    }
                },
            }
        )
        == "job-00000000000000000000000000aaa111"
    )


def test_resolve_elastic_blast_job_id_falls_back_to_progress_steps() -> None:
    payload = {
        "elastic_blast_job_id": None,
        "_progress": {
            "steps": {
                "running": {"k8s": {"job_id": "job-00000000000000000000000000ccc333"}},
                "exporting_results": {"k8s": {"job_id": "job-00000000000000000000000000ddd444"}},
            }
        },
    }
    assert k8s.resolve_elastic_blast_job_id(payload) == "job-00000000000000000000000000ccc333"


def test_resolve_elastic_blast_job_id_falls_back_to_external_k8s() -> None:
    payload = {
        "k8s_job_id": "",
        "external": {"k8s": {"job_id": "job-00000000000000000000000000eee555"}},
    }
    assert k8s.resolve_elastic_blast_job_id(payload) == "job-00000000000000000000000000eee555"


def test_resolve_elastic_blast_job_id_reads_external_elb_job_id() -> None:
    # The sibling /v1/jobs row exposes the elastic-blast job id directly as
    # ``external.elb_job_id`` (the dashboard stores the row under
    # ``payload.external``). This is what enables live pod-log streaming for
    # external / Service Bus jobs.
    payload = {
        "external": {
            "elb_job_id": "job-00000000000000000000000000fff666",
            "submission_source": "servicebus",
        }
    }
    assert k8s.resolve_elastic_blast_job_id(payload) == "job-00000000000000000000000000fff666"


def test_resolve_elastic_blast_job_id_prefers_durable_column() -> None:
    payload = {"external": {"elb_job_id": "job-0000000000000000000000000a1e0111"}}
    assert (
        k8s.resolve_elastic_blast_job_id(
            payload,
            persisted_job_id="job-000000000000000000000000d0ab1e22",
            error_text="Shard init jobs failed: init-ssd-deadbeef-3",
        )
        == "job-000000000000000000000000d0ab1e22"
    )


def test_resolve_elastic_blast_job_id_recovers_init_failure_suffix() -> None:
    error = "RuntimeError: Shard init jobs failed: init-ssd-d8faab8f-3"
    assert k8s.resolve_elastic_blast_job_id({}, error_text=error) == "job-d8faab8f"
    full_id = "job-0123456789abcdef0123456789abcdef"
    full_error = f"RuntimeError: Shard init jobs failed: init-ssd-{full_id}-3"
    assert k8s.resolve_elastic_blast_job_id({}, error_text=full_error) == full_id
    assert k8s.elastic_blast_selector_from_error("init-ssd-nothex-3") == ""
    assert k8s.elastic_blast_selector_from_error("init-ssd-d8faab8f-3000") == ""


def test_resolve_elastic_blast_job_id_returns_empty_when_missing() -> None:
    assert k8s.resolve_elastic_blast_job_id(None) == ""
    assert k8s.resolve_elastic_blast_job_id({}) == ""
    assert k8s.resolve_elastic_blast_job_id({"elastic_blast_job_id": "job-a"}) == ""
    assert (
        k8s.resolve_elastic_blast_job_id(
            {"elastic_blast_job_id": "not-a-job-id", "_progress": "string"}
        )
        == ""
    )


def test_pod_matching_rejects_embedded_suffix_collision() -> None:
    pod = {
        "metadata": {
            "name": "blastn-batch-s00-job-000-xdeadbeefx-abcde",
            "labels": {},
        },
        "spec": {"containers": [{"name": "blast"}]},
    }

    assert not k8s._pod_matches_job(
        pod,
        "dashboard-job",
        "job-000000000000000000000000deadbeef",
    )


def test_pod_matching_rejects_suffix_collision_with_mismatched_label() -> None:
    pod = {
        "metadata": {
            "name": "blastn-batch-s00-job-000-deadbeef-abcde",
            "labels": {"elb-job-id": "job-111111111111111111111111deadbeef"},
        },
        "spec": {"containers": [{"name": "blast"}]},
    }

    assert not k8s._pod_matches_job(
        pod,
        "dashboard-job",
        "job-000000000000000000000000deadbeef",
    )


def test_pod_matching_rejects_suffix_collision_with_mismatched_env() -> None:
    pod = {
        "metadata": {
            "name": "blastn-batch-s00-job-000-deadbeef-abcde",
            "labels": {},
        },
        "spec": {
            "containers": [
                {
                    "name": "blast",
                    "env": [
                        {
                            "name": "BLAST_ELB_JOB_ID",
                            "value": "job-111111111111111111111111deadbeef",
                        }
                    ],
                }
            ]
        },
    }

    assert not k8s._pod_matches_job(
        pod,
        "dashboard-job",
        "job-000000000000000000000000deadbeef",
    )


def test_pod_matching_rejects_unlabelled_full_id_suffix_match() -> None:
    pod = {
        "metadata": {
            "name": "blastn-batch-s00-job-000-deadbeef-abcde",
            "labels": {},
        },
        "spec": {"containers": [{"name": "blast"}]},
    }

    assert not k8s._pod_matches_job(
        pod,
        "dashboard-job",
        "job-000000000000000000000000deadbeef",
    )


def test_short_failure_selector_matches_only_exact_init_ssd_names() -> None:
    matching = {
        "metadata": {
            "name": "init-ssd-d8faab8f-3-abcde",
            "labels": {"elb-job-id": "job-000000000000000000000000d8faab8f"},
            "ownerReferences": [{"kind": "Job", "name": "init-ssd-d8faab8f-3"}],
        }
    }
    unrelated_batch = {
        "metadata": {
            "name": "blastn-batch-s00-job-000-d8faab8f-abcde",
            "labels": {"elb-job-id": "job-000000000000000000000000d8faab8f"},
        }
    }

    assert k8s._pod_matches_job(matching, "dashboard-job", "job-d8faab8f")
    assert not k8s._pod_matches_job(unrelated_batch, "dashboard-job", "job-d8faab8f")
