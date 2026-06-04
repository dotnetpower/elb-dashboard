"""Tests for the blue/green revision + traffic helpers (no live ARM).

Module summary: Constructs fake ARM-shaped Container App + revision
objects and exercises the traffic-weight mutations, serving-revision
resolution, label assignment, and revision activate/deactivate paths.

Responsibility: Verify `revisions` helpers mutate the ingress.traffic
  block correctly and never set a revision_suffix (so they cannot
  accidentally create a new revision).
Edit boundaries: Update when the traffic-block shape or revision SDK
  surface changes.
Key entry points: Tests for pin/cutover/flip/assign_label weight maths,
  serving_revision fallback, deactivate best-effort.
Risky contracts: Asserts cutover keeps blue ACTIVE at weight 0 (the
  instant-rollback invariant).
Validation: `uv run pytest -q api/tests/test_upgrade_revisions.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from api.services.upgrade import aca_template, revisions


@dataclass
class _Traffic:
    revision_name: str
    weight: int
    label: str | None = None


@dataclass
class _Ingress:
    traffic: list[Any] = field(default_factory=list)


@dataclass
class _Configuration:
    ingress: _Ingress
    secrets: Any | None = None


@dataclass
class _Template:
    revision_suffix: str = ""


@dataclass
class _Properties:
    configuration: _Configuration
    template: _Template = field(default_factory=_Template)
    latest_revision_name: str = "ca-elb-dashboard--green"


@dataclass
class _AppResource:
    properties: _Properties
    name: str = "ca-elb-dashboard"


@dataclass
class _RevProps:
    active: bool = True
    created_time: datetime | None = None
    running_state: str = "Running"
    provisioning_state: str = "Provisioned"
    template: Any | None = None


@dataclass
class _Container:
    image: str


@dataclass
class _RevTemplate:
    containers: list[Any] = field(default_factory=list)


@dataclass
class _Revision:
    name: str
    properties: _RevProps = field(default_factory=_RevProps)


def _make_app(traffic: list[_Traffic] | None = None) -> _AppResource:
    return _AppResource(
        properties=_Properties(
            configuration=_Configuration(ingress=_Ingress(traffic=list(traffic or [])))
        )
    )


class _FakeClient:
    def __init__(self, app: _AppResource, revs: list[_Revision] | None = None) -> None:
        self.app = app
        self.revs = revs or []
        self.update_calls: list[_AppResource] = []
        self.activated: list[str] = []
        self.deactivated: list[str] = []
        self.deactivate_raises = False
        self.container_apps = self
        self.container_apps_revisions = self

    def get(self, rg: str, name: str) -> _AppResource:
        return self.app

    def begin_update(self, rg: str, name: str, payload: _AppResource) -> str:
        self.update_calls.append(payload)
        return "poller"

    def list_revisions(self, rg: str, name: str) -> list[_Revision]:
        return self.revs

    def activate_revision(self, rg: str, name: str, revision: str) -> None:
        self.activated.append(revision)

    def deactivate_revision(self, rg: str, name: str, revision: str) -> None:
        if self.deactivate_raises:
            raise RuntimeError("boom")
        self.deactivated.append(revision)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(aca_template.AZURE_SUBSCRIPTION_ID_ENV, "sub-1")
    monkeypatch.setenv(aca_template.AZURE_RESOURCE_GROUP_ENV, "rg-elb")
    monkeypatch.setenv(aca_template.CONTAINER_APP_NAME_ENV, "ca-elb-dashboard")
    monkeypatch.setenv(aca_template.PLATFORM_ACR_NAME_ENV, "myacr")


def _traffic_of(payload: _AppResource) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for entry in payload.properties.configuration.ingress.traffic:
        rev = revisions._entry_revision(entry)
        out[rev] = (revisions._entry_weight(entry), revisions._entry_label(entry))
    return out


def test_pin_traffic_sends_100_to_one_revision() -> None:
    client = _FakeClient(_make_app())
    revisions.pin_traffic(revision_name="ca-elb-dashboard--blue", client=client)
    assert len(client.update_calls) == 1
    traffic = _traffic_of(client.update_calls[0])
    assert traffic == {"ca-elb-dashboard--blue": (100, "")}
    # Never sets a revision_suffix → never creates a new revision.
    assert client.update_calls[0].properties.template.revision_suffix == ""


def test_cutover_keeps_blue_active_at_zero() -> None:
    client = _FakeClient(_make_app([_Traffic("ca-elb-dashboard--blue", 100)]))
    revisions.cutover(
        green_revision="ca-elb-dashboard--green",
        blue_revision="ca-elb-dashboard--blue",
        client=client,
    )
    traffic = _traffic_of(client.update_calls[0])
    assert traffic["ca-elb-dashboard--green"] == (100, "green")
    # Instant-rollback invariant: blue stays in the traffic block at 0.
    assert traffic["ca-elb-dashboard--blue"] == (0, "blue")


def test_flip_traffic_restores_blue() -> None:
    client = _FakeClient(
        _make_app(
            [
                _Traffic("ca-elb-dashboard--green", 100, "green"),
                _Traffic("ca-elb-dashboard--blue", 0, "blue"),
            ]
        )
    )
    revisions.flip_traffic(
        to_revision="ca-elb-dashboard--blue",
        from_revision="ca-elb-dashboard--green",
        client=client,
    )
    traffic = _traffic_of(client.update_calls[0])
    assert traffic["ca-elb-dashboard--blue"][0] == 100
    assert traffic["ca-elb-dashboard--green"][0] == 0


def test_serving_revision_picks_highest_weight() -> None:
    client = _FakeClient(
        _make_app(
            [
                _Traffic("ca-elb-dashboard--green", 100, "green"),
                _Traffic("ca-elb-dashboard--blue", 0, "blue"),
            ]
        )
    )
    assert revisions.serving_revision(client=client) == "ca-elb-dashboard--green"


def test_serving_revision_falls_back_to_latest_when_no_traffic() -> None:
    client = _FakeClient(_make_app([]))
    assert revisions.serving_revision(client=client) == "ca-elb-dashboard--green"


def test_assign_label_moves_label_off_other_revision() -> None:
    client = _FakeClient(
        _make_app(
            [
                _Traffic("ca-elb-dashboard--blue", 100, "green"),  # stale label
                _Traffic("ca-elb-dashboard--new", 0),
            ]
        )
    )
    revisions.assign_label(revision_name="ca-elb-dashboard--new", label="green", client=client)
    traffic = _traffic_of(client.update_calls[0])
    assert traffic["ca-elb-dashboard--new"][1] == "green"
    assert traffic["ca-elb-dashboard--blue"][1] == ""  # stale label dropped


def test_list_revisions_joins_weight_and_label() -> None:
    now = datetime.now(UTC)
    client = _FakeClient(
        _make_app(
            [
                _Traffic("ca-elb-dashboard--green", 100, "green"),
                _Traffic("ca-elb-dashboard--blue", 0, "blue"),
            ]
        ),
        revs=[
            _Revision("ca-elb-dashboard--green", _RevProps(active=True, created_time=now)),
            _Revision("ca-elb-dashboard--blue", _RevProps(active=True, created_time=now)),
            _Revision("ca-elb-dashboard--old", _RevProps(active=False, created_time=now)),
        ],
    )
    summaries = {r.name: r for r in revisions.list_revisions(client=client)}
    assert summaries["ca-elb-dashboard--green"].weight == 100
    assert summaries["ca-elb-dashboard--green"].label == "green"
    assert summaries["ca-elb-dashboard--old"].weight == 0
    assert summaries["ca-elb-dashboard--old"].active is False


def test_deactivate_revision_best_effort_returns_false_on_error() -> None:
    client = _FakeClient(_make_app())
    client.deactivate_raises = True
    result = revisions.deactivate_revision(revision_name="ca-elb-dashboard--old", client=client)
    assert result is False


def test_deactivate_revision_success() -> None:
    client = _FakeClient(_make_app())
    result = revisions.deactivate_revision(revision_name="ca-elb-dashboard--old", client=client)
    assert result is True
    assert client.deactivated == ["ca-elb-dashboard--old"]


def test_revision_image_refs_extracts_per_revision() -> None:
    client = _FakeClient(
        _make_app(),
        revs=[
            _Revision(
                "ca-elb-dashboard--green",
                _RevProps(
                    template=_RevTemplate(
                        containers=[
                            _Container("myacr.azurecr.io/elb-api:v0.3.0"),
                            _Container("redis:7-alpine"),
                        ]
                    )
                ),
            ),
            _Revision(
                "ca-elb-dashboard--blue",
                _RevProps(
                    template=_RevTemplate(
                        containers=[_Container("myacr.azurecr.io/elb-api:v0.2.0")]
                    )
                ),
            ),
        ],
    )
    refs = revisions.revision_image_refs(client=client)
    assert refs["ca-elb-dashboard--green"] == {
        "myacr.azurecr.io/elb-api:v0.3.0",
        "redis:7-alpine",
    }
    assert refs["ca-elb-dashboard--blue"] == {"myacr.azurecr.io/elb-api:v0.2.0"}
