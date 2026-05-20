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


class _FakeRegistries:
    def __init__(self) -> None:
        self.request = None

    def begin_schedule_run(
        self, _resource_group: str, _registry_name: str, request: object
    ) -> None:
        self.request = request


class _FakeManagementClient:
    def __init__(self) -> None:
        self.registries = _FakeRegistries()


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
