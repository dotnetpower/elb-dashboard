"""Tests for Job Log Kubernetes behavior.

Responsibility: Tests for Job Log Kubernetes behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_discover_k8s_log_targets_filters_to_job_and_maps_phases`,
`test_stream_k8s_log_lines_uses_following_pod_log_api`
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
                            "labels": {},
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
                                        {"name": "BLAST_ELB_JOB_ID", "value": "job-abcde2fc8081"}
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
        elastic_job_id="job-abcde2fc8081",
    )

    assert [(target.pod_name, target.container_name, target.phase) for target in targets] == [
        ("blastn-batch-s00-job-000-e2fc8081-abcde", "blast", "running"),
        ("init-ssd-e2fc8081-0-abcde", "get-blastdb", "staging_db"),
        ("init-ssd-e2fc8081-0-abcde", "import-query-batches", "staging_db"),
    ]


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
