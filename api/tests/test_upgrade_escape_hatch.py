"""Tests for the escape-hatch command generator.

Module summary: Asserts the generated `az containerapp update` commands
target each upgraded container, include subscription/RG/app metadata,
and never embed secrets.

Responsibility: Verify command shape + secret hygiene.
Edit boundaries: Update when the command template changes.
Key entry points: Tests for full plan, missing env fallbacks,
  per-container coverage, secret absence.
Risky contracts: Asserts the commands themselves never contain a token
  / SAS / hex secret — defence against future regressions.
Validation: `uv run pytest -q api/tests/test_upgrade_escape_hatch.py`.
"""

from __future__ import annotations

import pytest
from api.services.upgrade import aca_template, escape_hatch


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-deadbeef")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")


def _images() -> aca_template.SidecarImages:
    return aca_template.SidecarImages(
        api="myacr.azurecr.io/elb-api:v0.2.1",
        frontend="myacr.azurecr.io/elb-frontend:v0.2.1",
        terminal="myacr.azurecr.io/elb-terminal:v0.2.1",
    )


def test_plan_covers_each_container() -> None:
    plan = escape_hatch.build_plan(_images())
    cmds = "\n".join(plan.commands)
    for container in ("api", "worker", "beat", "frontend", "terminal"):
        assert f"--container-name {container}" in cmds
    # Every command carries the subscription explicitly so the operator's
    # default profile is never mutated by running them.
    for cmd in plan.commands:
        assert "--subscription sub-deadbeef" in cmd
    assert all(not cmd.startswith("az account") for cmd in plan.commands)


def test_plan_includes_resource_group_and_app() -> None:
    plan = escape_hatch.build_plan(_images())
    for cmd in plan.commands:
        assert "--resource-group rg-elb" in cmd
        assert "--name ca-elb-dashboard" in cmd


def test_plan_omits_secrets() -> None:
    plan = escape_hatch.build_plan(_images())
    blob = "\n".join(plan.commands).lower()
    for needle in ("token", "secret", "sas", "bearer"):
        assert needle not in blob


def test_plan_falls_back_to_placeholders_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(aca_template.CONTAINER_APP_NAME_ENV, raising=False)
    monkeypatch.delenv(aca_template.AZURE_RESOURCE_GROUP_ENV, raising=False)
    plan = escape_hatch.build_plan(_images())
    assert "<container-app>" in "\n".join(plan.commands)
    assert "<resource-group>" in "\n".join(plan.commands)
