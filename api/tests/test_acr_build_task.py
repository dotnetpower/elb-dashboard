"""Tests for ACR Build Task behavior.

Responsibility: Tests for ACR Build Task behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_FakeRegistries`, `_FakeManagementClient`, `_scheduled_task_yaml`,
`test_job_submit_build_patches_azure_snapshot_flow`, `test_pre_build_command_is_shell_quoted`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_acr_build_task.py`.
"""

from __future__ import annotations

import base64

from api.services.image_tags import IMAGE_BUILD_INFO
from api.tasks.acr import _schedule_acr_build


class _FakeRun:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


class _FakePollingMethod:
    def __init__(self, run: _FakeRun | None) -> None:
        self._run = run

    def resource(self) -> _FakeRun | None:
        return self._run


class _FakePoller:
    def __init__(self, run: _FakeRun | None) -> None:
        self._method = _FakePollingMethod(run)

    def polling_method(self) -> _FakePollingMethod:
        return self._method


class _FakeRegistries:
    def __init__(self, run_id: str | None = "ca42") -> None:
        self.request = None
        self._run_id = run_id

    def begin_schedule_run(
        self, _resource_group: str, _registry_name: str, request: object
    ) -> _FakePoller:
        self.request = request
        return _FakePoller(_FakeRun(self._run_id) if self._run_id else None)


class _FakeManagementClient:
    def __init__(self, run_id: str | None = "ca42") -> None:
        self.registries = _FakeRegistries(run_id=run_id)


def _scheduled_task_yaml() -> str:
    mgmt = _FakeManagementClient()
    _schedule_acr_build(
        mgmt,
        "rg-acr",
        "acr1",
        "ncbi/elasticblast-job-submit",
        "4.1.0",
        IMAGE_BUILD_INFO["ncbi/elasticblast-job-submit"],
    )
    assert mgmt.registries.request is not None
    encoded = mgmt.registries.request.encoded_task_content
    return base64.b64decode(encoded).decode("utf-8")


def test_job_submit_build_patches_azure_snapshot_flow() -> None:
    task_yaml = _scheduled_task_yaml()

    assert "cp -r src/elastic_blast/templates docker-job-submit/" in task_yaml
    assert "COPY templates/ /templates/" in task_yaml
    assert "x${ELB_CLOUD_PROVIDER:-azure} = xgcp" in task_yaml


def test_pre_build_command_is_shell_quoted() -> None:
    task_yaml = _scheduled_task_yaml()

    assert "bash -lc '" in task_yaml
    assert "'\"'\"'s|COPY templates" in task_yaml


def test_schedule_returns_run_id_from_initial_response() -> None:
    """`_schedule_acr_build` must surface the queued Run's run_id without
    blocking on the LRO — callers persist it for the ACR card's "Building"
    state mapping.
    """
    mgmt = _FakeManagementClient(run_id="ca123")
    run_id = _schedule_acr_build(
        mgmt,
        "rg-acr",
        "acr1",
        "elb-openapi",
        "4.14",
        IMAGE_BUILD_INFO["elb-openapi"],
    )
    assert run_id == "ca123"


def test_schedule_tolerates_missing_run_id() -> None:
    """When the poller doesn't expose a Run yet (older SDK shapes), the
    helper returns None and the caller skips the persistence write.
    """
    mgmt = _FakeManagementClient(run_id=None)
    run_id = _schedule_acr_build(
        mgmt,
        "rg-acr",
        "acr1",
        "elb-openapi",
        "4.14",
        IMAGE_BUILD_INFO["elb-openapi"],
    )
    assert run_id is None
