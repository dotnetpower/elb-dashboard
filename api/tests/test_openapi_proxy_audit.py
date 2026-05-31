"""Tests for the OpenAPI proxy forensic audit helper.

Responsibility: Verify ``record_openapi_proxy_exec`` writes a well-formed,
    token-free audit row for state-changing proxy calls and that
    ``is_state_changing_method`` classifies verbs correctly.
Edit boundaries: No network, Kubernetes, or real Redis. Patch
    ``api.services.state_repo.get_state_repo`` with a fake.
Key entry points: ``test_*``.
Risky contracts: The audit row must never carry a token value and must use
    ``owner_oid="system"`` when no caller is supplied.
Validation: ``uv run pytest -q api/tests/test_openapi_proxy_audit.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.openapi.proxy_audit import (
    is_state_changing_method,
    record_openapi_proxy_exec,
)


class _FakeRepo:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.history: list[tuple[str, str, dict[str, Any]]] = []

    def create(self, job: Any) -> None:
        self.created.append(job)

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append((job_id, event, payload))


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("POST", True),
        ("post", True),
        ("PUT", True),
        ("PATCH", True),
        ("DELETE", True),
        ("GET", False),
        ("get", False),
        ("HEAD", False),
        ("OPTIONS", False),
    ],
)
def test_is_state_changing_method(method: str, expected: bool) -> None:
    assert is_state_changing_method(method) is expected


def test_record_writes_token_free_row(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    record_openapi_proxy_exec(
        method="post",
        target_path="/v1/jobs?db=nt",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        caller_oid="caller-oid-1",
        tenant_id="tenant-1",
    )

    assert len(repo.created) == 1
    job = repo.created[0]
    assert job.type == "openapi_proxy_exec"
    assert job.owner_oid == "caller-oid-1"
    assert job.tenant_id == "tenant-1"
    assert job.job_id.startswith("openapi-proxy:POST:aks-elb:")
    assert job.payload["method"] == "POST"
    assert job.payload["target_path"] == "/v1/jobs?db=nt"
    # Forensic rows must never carry a token value.
    serialized = repr(job.payload)
    assert "token" not in serialized.lower()
    assert len(repo.history) == 1


def test_record_defaults_owner_to_system(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    record_openapi_proxy_exec(
        method="DELETE",
        target_path="/v1/jobs/abc",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert repo.created[0].owner_oid == "system"
    assert repo.created[0].tenant_id == ""


def test_record_caps_path_length(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    long_path = "/v1/jobs?" + ("x" * 2000)
    record_openapi_proxy_exec(
        method="POST",
        target_path=long_path,
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert len(repo.created[0].payload["target_path"]) == 512


def test_record_swallows_repo_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise RuntimeError("repo down")

    monkeypatch.setattr("api.services.state_repo.get_state_repo", _boom)

    # Must not raise — audit append is best-effort.
    record_openapi_proxy_exec(
        method="POST",
        target_path="/v1/jobs",
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )
