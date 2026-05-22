"""Tests for the ACA template helper (no live ARM).

Module summary: Constructs fake ARM-shaped Container App objects and
exercises image extraction / template mutation / image swap logic.

Responsibility: Verify SidecarImages snapshot + swap behaviour.
Edit boundaries: Update when the template shape or container naming
  contract changes.
Key entry points: Tests for happy-path snapshot, missing container,
  swap_images mutation + computed target image refs.
Risky contracts: Asserts api/worker/beat all share the elb-api image
  role so a refactor that splits them out is loud.
Validation: `uv run pytest -q api/tests/test_upgrade_aca_template.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from api.services.upgrade import aca_template


@dataclass
class _Container:
    name: str
    image: str


@dataclass
class _Template:
    containers: list[_Container]
    revision_suffix: str = ""


@dataclass
class _Properties:
    template: _Template
    latest_revision_name: str = "ca-elb-dashboard--rev1"


@dataclass
class _AppResource:
    properties: _Properties
    name: str = "ca-elb-dashboard"


def _make_app(image_tag: str = "v0.2.1") -> _AppResource:
    acr = "myacr.azurecr.io"
    return _AppResource(
        properties=_Properties(
            template=_Template(
                containers=[
                    _Container("api", f"{acr}/elb-api:{image_tag}"),
                    _Container("worker", f"{acr}/elb-api:{image_tag}"),
                    _Container("beat", f"{acr}/elb-api:{image_tag}"),
                    _Container("frontend", f"{acr}/elb-frontend:{image_tag}"),
                    _Container("terminal", f"{acr}/elb-terminal:{image_tag}"),
                    _Container("redis", "redis:7-alpine"),  # untouched
                ]
            )
        )
    )


class _FakeClient:
    def __init__(self, app: _AppResource) -> None:
        self.app = app
        self.get_calls = 0
        self.update_calls: list[Any] = []
        self.container_apps = self
        self.container_apps_revisions = self

    def get(self, rg: str, name: str) -> _AppResource:
        self.get_calls += 1
        return self.app

    def begin_update(self, rg: str, name: str, payload: _AppResource) -> str:
        self.update_calls.append(payload)
        return "poller-handle"

    def get_revision(self, rg: str, app: str, revision: str) -> Any:
        # Used only by rollout_watcher tests; default not-running.
        return type(
            "Rev",
            (),
            {
                "name": revision,
                "properties": type(
                    "Props",
                    (),
                    {
                        "running_state": "Processing",
                        "provisioning_state": "Provisioning",
                        "health_state": "Unknown",
                    },
                )(),
            },
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")
    monkeypatch.setenv(aca_template.PLATFORM_ACR_NAME_ENV, "myacr")


def test_read_current_images_extracts_per_role() -> None:
    client = _FakeClient(_make_app("v0.2.1"))
    images = aca_template.read_current_images(client=client)
    assert images.api == "myacr.azurecr.io/elb-api:v0.2.1"
    assert images.frontend == "myacr.azurecr.io/elb-frontend:v0.2.1"
    assert images.terminal == "myacr.azurecr.io/elb-terminal:v0.2.1"


def test_read_current_images_raises_when_required_container_missing() -> None:
    client = _FakeClient(_AppResource(properties=_Properties(template=_Template(containers=[
        _Container("api", "x:1"),
        _Container("frontend", "y:1"),
    ]))))
    with pytest.raises(aca_template.TemplateError):
        aca_template.read_current_images(client=client)


def test_swap_images_mutates_each_role_container() -> None:
    app = _make_app("v0.2.1")
    client = _FakeClient(app)
    poller, previous, target = aca_template.swap_images(
        target_version="0.3.0",
        revision_suffix="v0-3-0-abc",
        client=client,
    )
    assert poller == "poller-handle"
    assert previous.api.endswith(":v0.2.1")
    assert target.api == "myacr.azurecr.io/elb-api:v0.3.0"
    # api, worker, beat all rewritten; redis untouched.
    for container in app.properties.template.containers:
        if container.name in {"api", "worker", "beat"}:
            assert container.image == "myacr.azurecr.io/elb-api:v0.3.0"
        elif container.name == "frontend":
            assert container.image == "myacr.azurecr.io/elb-frontend:v0.3.0"
        elif container.name == "terminal":
            assert container.image == "myacr.azurecr.io/elb-terminal:v0.3.0"
        elif container.name == "redis":
            assert container.image == "redis:7-alpine"
    assert app.properties.template.revision_suffix == "v0-3-0-abc"


def test_apply_images_writes_explicit_refs() -> None:
    app = _make_app("v0.3.0")
    client = _FakeClient(app)
    aca_template.apply_images(
        images=aca_template.SidecarImages(
            api="myacr.azurecr.io/elb-api:v0.2.1",
            frontend="myacr.azurecr.io/elb-frontend:v0.2.1",
            terminal="myacr.azurecr.io/elb-terminal:v0.2.1",
        ),
        revision_suffix="rb-20260522",
        client=client,
    )
    for container in app.properties.template.containers:
        if container.name in {"api", "worker", "beat"}:
            assert container.image == "myacr.azurecr.io/elb-api:v0.2.1"
    assert app.properties.template.revision_suffix == "rb-20260522"
