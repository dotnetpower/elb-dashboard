"""Tests for list_acr_repositories pending-builds fallback.

Responsibility: Verify the ACR card monitoring endpoint surfaces
in-progress builds even when ACR Run.output_images is still empty, by
falling back to the persisted run_id -> image:tag mapping captured at
build submission time.
Edit boundaries: Keep assertions focused on the behaviour under test;
prefer fakes over live Azure calls.
Key entry points: `test_pending_runs_without_output_images_surface_as_building`,
`test_succeeded_run_prunes_pending_entry`.
Risky contracts: Do not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_acr_monitoring_pending.py`.
"""

from __future__ import annotations

from typing import Any

import api.services.monitoring as monitoring_svc
import pytest


class _FakeImageDescriptor:
    def __init__(self, repository: str, tag: str) -> None:
        self.repository = repository
        self.tag = tag


class _FakeRun:
    def __init__(
        self,
        run_id: str,
        status: str,
        output_images: list[_FakeImageDescriptor] | None = None,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.output_images = output_images


class _FakeRunsOperations:
    def __init__(self, runs: list[_FakeRun]) -> None:
        self._runs = runs

    def list(self, _rg: str, _registry: str) -> list[_FakeRun]:
        return list(self._runs)


class _FakePreviewClient:
    def __init__(self, runs: list[_FakeRun]) -> None:
        self.runs = _FakeRunsOperations(runs)


class _FakeSku:
    name = "Premium"


class _FakeRegistry:
    def __init__(self) -> None:
        self.name = "acr1"
        self.login_server = "acr1.azurecr.io"
        self.sku = _FakeSku()


class _FakeRegistries:
    def get(self, _rg: str, _name: str) -> _FakeRegistry:
        return _FakeRegistry()


class _FakeMgmtClient:
    def __init__(self) -> None:
        self.registries = _FakeRegistries()


@pytest.fixture
def patched_acr(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install fakes for acr_client + ContainerRegistryManagementClient."""

    def _fake_acr_client(*_args: Any, **_kwargs: Any) -> _FakeMgmtClient:
        return _FakeMgmtClient()

    monkeypatch.setattr(monitoring_svc, "acr_client", _fake_acr_client)

    state: dict[str, Any] = {"runs": [], "pruned": []}

    class _Constructor:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._client = _FakePreviewClient(state["runs"])
            self.runs = self._client.runs

    import azure.mgmt.containerregistry as acr_pkg

    monkeypatch.setattr(
        acr_pkg, "ContainerRegistryManagementClient", _Constructor
    )

    from api.services import acr_build_state

    def _fake_load(_registry: str) -> dict[str, dict[str, str]]:
        return dict(state.get("pending", {}))

    def _fake_prune(_registry: str, ids: set[str]) -> None:
        state["pruned"].append(set(ids))

    monkeypatch.setattr(acr_build_state, "load_pending_builds", _fake_load)
    monkeypatch.setattr(acr_build_state, "prune_terminal_builds", _fake_prune)

    return state


def test_pending_runs_without_output_images_surface_as_building(
    patched_acr: dict[str, Any],
) -> None:
    """A Queued/Running run whose output_images is still empty must be
    surfaced via the persisted run_id -> image:tag mapping so the SPA
    can render the per-image "Building" indicator after a browser
    refresh.
    """
    patched_acr["runs"] = [
        _FakeRun("ca10", "Running", output_images=None),
    ]
    patched_acr["pending"] = {
        "ca10": {
            "image": "ncbi/elasticblast-job-submit",
            "tag": "4.1.0",
            "created_at": "2026-05-23T10:00:00+00:00",
        }
    }

    data = monitoring_svc.list_acr_repositories(
        credential=object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr1",
    )

    assert "ncbi/elasticblast-job-submit:4.1.0" in data["building_images"]
    detail = next(
        d for d in data["build_details"] if d["image"].startswith("ncbi/elasticblast-job-submit")
    )
    assert detail["status"] == "Running"
    assert detail["run_id"] == "ca10"


def test_succeeded_run_prunes_pending_entry(patched_acr: dict[str, Any]) -> None:
    """When a previously-pending run completes, the stale row is best-effort
    pruned from the persisted mapping so the table doesn't grow forever.
    """
    patched_acr["runs"] = [
        _FakeRun(
            "ca7",
            "Succeeded",
            output_images=[_FakeImageDescriptor("ncbi/elb", "1.4.0")],
        ),
    ]
    patched_acr["pending"] = {
        "ca7": {"image": "ncbi/elb", "tag": "1.4.0", "created_at": ""},
    }

    monitoring_svc.list_acr_repositories(
        credential=object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr1",
    )

    assert patched_acr["pruned"], "expected prune_terminal_builds to be invoked"
    assert "ca7" in patched_acr["pruned"][0]
