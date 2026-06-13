"""Unit tests for the Deployment/Job logs, describe, and delete helpers.

Verifies the system-namespace gate, name validation, status-code mapping,
representative-pod selection for logs, and describe formatting in isolation by
stubbing `_get_k8s_session` so no Kubernetes API is contacted.

Responsibility: Cover the safety + response-shape contracts of
`api.services.k8s.workload_ops`.
Edit boundaries: Pure unit tests — no real credentials, no real K8s API.
Key entry points: the `test_*` functions below.
Risky contracts: The system-namespace delete gate is load-bearing — the
frontend also hides the button, but the backend must remain the authoritative
gate (OWASP A01). Log helpers must aggregate every matching pod (Running
first, then newest) and every container of each pod, capped at
`_MAX_LOG_PODS`, so a fan-out Job's output is shown in full.
Validation: `uv run pytest -q api/tests/test_k8s_workload_ops.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from api.services.k8s import workload_ops as wl


class _FakeResponse:
    def __init__(
        self, status_code: int = 200, *, json_body: Any = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _FakeSession:
    """Routes GET/DELETE calls to canned responses keyed by URL substring."""

    def __init__(self, get_routes: dict[str, _FakeResponse] | None = None) -> None:
        self._get_routes = get_routes or {}
        self.delete_response = _FakeResponse(202)
        self.closed = False
        self.get_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def get(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: int = 10
    ) -> _FakeResponse:
        self.get_calls.append({"url": url, "params": params, "timeout": timeout})
        # Match the most specific (longest) registered suffix so a pod-list
        # URL (".../pods") and a pod-log URL (".../pods/<n>/log") don't collide.
        for needle in sorted(self._get_routes, key=len, reverse=True):
            if url.endswith(needle):
                return self._get_routes[needle]
        return _FakeResponse(200, json_body={})

    def delete(
        self, url: str, *, params: dict[str, Any], timeout: int
    ) -> _FakeResponse:
        self.delete_calls.append({"url": url, "params": params, "timeout": timeout})
        return self.delete_response

    def close(self) -> None:
        self.closed = True


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> _FakeSession:
    def _fake_get_session(*_args: Any, **_kwargs: Any) -> tuple[_FakeSession, str]:
        return session, "https://example.test"

    # workload_ops imports lazily from api.services.k8s.monitoring, so patch
    # the symbol on that module.
    from api.services.k8s import monitoring as monitoring_module

    monkeypatch.setattr(monitoring_module, "_get_k8s_session", _fake_get_session)
    return session


_CRED = SimpleNamespace()


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("namespace", sorted(wl.SYSTEM_NAMESPACES))
def test_deployment_delete_refuses_system_namespaces(namespace: str) -> None:
    with pytest.raises(PermissionError):
        wl.k8s_deployment_delete(
            credential=_CRED,  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace=namespace,
            deployment_name="dep",
        )


@pytest.mark.parametrize("namespace", sorted(wl.SYSTEM_NAMESPACES))
def test_job_delete_refuses_system_namespaces(namespace: str) -> None:
    with pytest.raises(PermissionError):
        wl.k8s_job_delete(
            credential=_CRED,  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace=namespace,
            job_name="job",
        )


def test_delete_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        wl.k8s_deployment_delete(
            credential=_CRED,  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace="UPPER",  # not RFC 1123
            deployment_name="dep",
        )


def test_deployment_delete_deleted_uses_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_session(monkeypatch, _FakeSession())
    session.delete_response = _FakeResponse(202)
    result = wl.k8s_deployment_delete(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        deployment_name="my-dep",
    )
    assert result["status"] == "deleted"
    assert result["kind"] == "Deployment"
    assert result["namespace"] == "default"
    assert result["name"] == "my-dep"
    assert result["status_code"] == 202
    assert session.closed is True
    call = session.delete_calls[0]
    assert call["url"].endswith("/apis/apps/v1/namespaces/default/deployments/my-dep")
    assert call["params"]["propagationPolicy"] == "Foreground"


def test_job_delete_deleted_uses_background(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _patch_session(monkeypatch, _FakeSession())
    session.delete_response = _FakeResponse(200)
    result = wl.k8s_job_delete(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="elb-search-0",
    )
    assert result["status"] == "deleted"
    assert result["kind"] == "Job"
    assert result["name"] == "elb-search-0"
    call = session.delete_calls[0]
    assert call["url"].endswith("/apis/batch/v1/namespaces/blast/jobs/elb-search-0")
    assert call["params"]["propagationPolicy"] == "Background"


def test_delete_not_found_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _patch_session(monkeypatch, _FakeSession())
    session.delete_response = _FakeResponse(404, text="not found")
    result = wl.k8s_deployment_delete(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        deployment_name="missing",
    )
    assert result["status"] == "not_found"
    assert result["status_code"] == 404


def test_delete_propagates_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _patch_session(monkeypatch, _FakeSession())
    session.delete_response = _FakeResponse(500, text="kube-apiserver exploded")
    result = wl.k8s_job_delete(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        job_name="some-job",
    )
    assert result["status"] == "error"
    assert result["status_code"] == 500
    assert "kube-apiserver exploded" in result["detail"]


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #


def test_deployment_logs_selects_running_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment_body = {
        "spec": {"selector": {"matchLabels": {"app": "web", "tier": "frontend"}}}
    }
    pods_body = {
        "items": [
            {
                "metadata": {"name": "old-pod", "creationTimestamp": "2020-01-01T00:00:00Z"},
                "status": {"phase": "Succeeded"},
            },
            {
                "metadata": {"name": "run-pod", "creationTimestamp": "2019-01-01T00:00:00Z"},
                "status": {"phase": "Running"},
            },
        ]
    }
    session = _patch_session(
        monkeypatch,
        _FakeSession(
            {
                "/deployments/my-dep": _FakeResponse(json_body=deployment_body),
                "/pods": _FakeResponse(json_body=pods_body),
                "/pods/run-pod/log": _FakeResponse(text="hello logs"),
            }
        ),
    )
    out = wl.k8s_deployment_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        deployment_name="my-dep",
    )
    # Running pod is shown first, but every matching pod is aggregated now.
    assert out.startswith("# logs from pod run-pod")
    assert "hello logs" in out
    assert "# logs from pod old-pod" in out
    # The label selector must be derived from the deployment matchLabels.
    pod_list_call = next(
        c for c in session.get_calls if c["params"] and "labelSelector" in c["params"]
    )
    assert "app=web" in pod_list_call["params"]["labelSelector"]


def test_deployment_logs_no_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(
        monkeypatch,
        _FakeSession({"/deployments/d": _FakeResponse(json_body={"spec": {}})}),
    )
    out = wl.k8s_deployment_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        deployment_name="d",
    )
    assert "no pod selector" in out


def test_job_logs_uses_job_name_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    pods_body = {
        "items": [
            {
                "metadata": {"name": "job-pod-xyz", "creationTimestamp": "2021-01-01T00:00:00Z"},
                "status": {"phase": "Succeeded"},
            }
        ]
    }
    session = _patch_session(
        monkeypatch,
        _FakeSession(
            {
                "/pods": _FakeResponse(json_body=pods_body),
                "/pods/job-pod-xyz/log": _FakeResponse(text="job output"),
            }
        ),
    )
    out = wl.k8s_job_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="elb-search-0",
    )
    assert out.startswith("# logs from pod job-pod-xyz")
    assert "job output" in out
    pod_list_call = next(
        c for c in session.get_calls if c["params"] and "labelSelector" in c["params"]
    )
    assert pod_list_call["params"]["labelSelector"] == "job-name=elb-search-0"


def test_job_logs_no_pods(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(
        monkeypatch,
        _FakeSession({"/pods": _FakeResponse(json_body={"items": []})}),
    )
    out = wl.k8s_job_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="empty-job",
    )
    assert "no pods found" in out


def test_logs_reject_invalid_name() -> None:
    with pytest.raises(ValueError):
        wl.k8s_job_logs(
            credential=_CRED,  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace="default",
            job_name="Bad Name",
        )


def test_job_logs_aggregates_all_pods(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fan-out Job's pods are all shown, Running first, then newest."""
    pods_body = {
        "items": [
            {
                "metadata": {"name": "p-old", "creationTimestamp": "2021-01-01T00:00:00Z"},
                "status": {"phase": "Succeeded"},
            },
            {
                "metadata": {"name": "p-run", "creationTimestamp": "2020-01-01T00:00:00Z"},
                "status": {"phase": "Running"},
            },
        ]
    }
    _patch_session(
        monkeypatch,
        _FakeSession(
            {
                "/pods": _FakeResponse(json_body=pods_body),
                "/pods/p-run/log": _FakeResponse(text="run output"),
                "/pods/p-old/log": _FakeResponse(text="old output"),
            }
        ),
    )
    out = wl.k8s_job_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="elb-search-0",
    )
    # Both pods are present and the Running pod's block comes first.
    assert out.startswith("# logs from pod p-run")
    assert "run output" in out
    assert "# logs from pod p-old" in out
    assert "old output" in out


def test_job_logs_caps_pod_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """More pods than `_MAX_LOG_PODS` are capped with an omission marker."""
    total = wl._MAX_LOG_PODS + 5
    pods_body = {
        "items": [
            {
                "metadata": {
                    "name": f"p-{i:03d}",
                    "creationTimestamp": f"2021-01-{(i % 28) + 1:02d}T00:00:00Z",
                },
                "status": {"phase": "Succeeded"},
            }
            for i in range(total)
        ]
    }
    _patch_session(
        monkeypatch,
        _FakeSession({"/pods": _FakeResponse(json_body=pods_body)}),
    )
    out = wl.k8s_job_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="big-job",
    )
    assert out.count("# logs from pod ") == wl._MAX_LOG_PODS
    assert "5 more pod(s) not shown" in out


def test_pod_logs_aggregate_all_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-container pod shows every container's log, init first."""
    pods_body = {
        "items": [
            {
                "metadata": {"name": "p-1", "creationTimestamp": "2021-01-01T00:00:00Z"},
                "status": {"phase": "Running"},
            }
        ]
    }
    pod_spec = {
        "spec": {
            "initContainers": [{"name": "fetch-db"}],
            "containers": [{"name": "blast"}],
        }
    }
    _patch_session(
        monkeypatch,
        _FakeSession(
            {
                "/pods": _FakeResponse(json_body=pods_body),
                "/pods/p-1/log": _FakeResponse(text="container output"),
                "/pods/p-1": _FakeResponse(json_body=pod_spec),
            }
        ),
    )
    out = wl.k8s_job_logs(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="multi-container-job",
    )
    assert "# logs from pod p-1" in out
    # Init container is listed before the main container.
    assert out.index("--- container: fetch-db ---") < out.index("--- container: blast ---")
    assert out.count("container output") == 2


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #


def test_deployment_describe_formats_replicas(monkeypatch: pytest.MonkeyPatch) -> None:
    dep_body = {
        "metadata": {
            "name": "web",
            "namespace": "default",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "labels": {"app": "web"},
        },
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "web"}},
            "strategy": {"type": "RollingUpdate"},
        },
        "status": {
            "updatedReplicas": 3,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "unavailableReplicas": 1,
            "conditions": [
                {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}
            ],
        },
    }
    events_body = {
        "items": [
            {
                "type": "Warning",
                "reason": "FailedCreate",
                "message": "quota exceeded",
                "lastTimestamp": "2026-01-01T00:05:00Z",
                "count": 4,
            }
        ]
    }
    _patch_session(
        monkeypatch,
        _FakeSession(
            {
                "/deployments/web": _FakeResponse(json_body=dep_body),
                "/events": _FakeResponse(json_body=events_body),
            }
        ),
    )
    out = wl.k8s_deployment_describe(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="default",
        deployment_name="web",
    )
    assert "Name:" in out and "web" in out
    assert "3 desired" in out
    assert "2 ready" in out
    assert "Strategy:" in out and "RollingUpdate" in out
    assert "MinimumReplicasUnavailable" in out
    assert "FailedCreate" in out
    assert "quota exceeded" in out


def test_job_describe_formats_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    job_body = {
        "metadata": {"name": "elb-search-0", "namespace": "blast"},
        "spec": {"parallelism": 4, "completions": 4},
        "status": {
            "active": 2,
            "succeeded": 1,
            "failed": 0,
            "startTime": "2026-01-01T00:00:00Z",
        },
    }
    _patch_session(
        monkeypatch,
        _FakeSession(
            {
                "/jobs/elb-search-0": _FakeResponse(json_body=job_body),
                "/events": _FakeResponse(json_body={"items": []}),
            }
        ),
    )
    out = wl.k8s_job_describe(
        credential=_CRED,  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        cluster_name="cluster",
        namespace="blast",
        job_name="elb-search-0",
    )
    assert "Parallelism:" in out
    assert "Completions:" in out
    assert "Active:" in out
    assert "Succeeded:" in out
    assert "Events:" in out and "<none>" in out


def test_describe_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        wl.k8s_deployment_describe(
            credential=_CRED,  # type: ignore[arg-type]
            subscription_id="sub",
            resource_group="rg",
            cluster_name="cluster",
            namespace="default",
            deployment_name="Bad Name",
        )
