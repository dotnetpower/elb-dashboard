"""Tests for the AKS Container Insights enable task's RBAC self-heal + retry.

Responsibility: Cover the linked-scope RBAC self-heal and bounded
`LinkedAuthorizationFailed` retry wired into `enable_aks_container_insights`.
Edit boundaries: Pure unit tests; monkeypatch the service helper, the facade
self-grant, credential, progress publisher, and `time.sleep`. No network.
Key entry points: `test_*` functions below.
Risky contracts: Must not perform real sleeps or Azure calls. Assertions
focus on (a) the workspace RG is parsed and self-granted, (b) the retry loop
is bounded, (c) exhaustion raises an actionable recovery command.
Validation: `uv run pytest -q api/tests/test_aks_observability_task.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks import azure
from api.tasks.azure import aks_observability as task_mod

_WORKSPACE_ID = (
    "/subscriptions/sub-1/resourcegroups/defaultresourcegroup-se/providers/"
    "microsoft.operationalinsights/workspaces/defaultworkspace-sub-1-se"
)


class _LinkedAuthError(Exception):
    """Mimics the ARM (LinkedAuthorizationFailed) error string."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "(LinkedAuthorizationFailed) The client has permission ..."


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch) -> None:
    monkeypatch.setattr(task_mod, "get_credential", lambda: object())
    monkeypatch.setattr(task_mod, "publish_progress", lambda *_a, **_kw: None)
    monkeypatch.setattr(task_mod.time, "sleep", lambda _s: None)


def _run(**overrides: Any) -> dict[str, Any]:
    kwargs = {
        "subscription_id": "sub-1",
        "resource_group": "rg-aks",
        "cluster_name": "aks1",
        "workspace_resource_id": _WORKSPACE_ID,
    }
    kwargs.update(overrides)
    return task_mod.enable_aks_container_insights.run(**kwargs)


def test_enable_self_grants_workspace_rg_then_succeeds(monkeypatch) -> None:
    grant_calls: list[dict[str, Any]] = []

    def fake_grant(_cred, *, subscription_id, resource_group, **_kw):
        grant_calls.append({"sub": subscription_id, "rg": resource_group})
        return {
            "roles_assigned": ["Contributor"],
            "roles_failed": {},
            "mi_principal_id": "mi-oid",
            "resource_group": resource_group,
        }

    monkeypatch.setattr(
        azure, "_ensure_dashboard_mi_resource_group_contributor", fake_grant
    )
    monkeypatch.setattr(
        task_mod,
        "enable_container_insights",
        lambda *_a, **_kw: {"enabled": True, "workspace_resource_id": _WORKSPACE_ID},
    )

    state = _run()

    assert state["enabled"] is True
    assert grant_calls == [{"sub": "sub-1", "rg": "defaultresourcegroup-se"}]


def test_enable_retries_on_linked_auth_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(
        azure,
        "_ensure_dashboard_mi_resource_group_contributor",
        lambda *_a, **_kw: {
            "roles_assigned": ["Contributor"],
            "roles_failed": {},
            "mi_principal_id": "mi-oid",
        },
    )

    calls = {"n": 0}

    def flaky_enable(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _LinkedAuthError()
        return {"enabled": True}

    monkeypatch.setattr(task_mod, "enable_container_insights", flaky_enable)

    state = _run()

    assert state["enabled"] is True
    assert calls["n"] == 2


def test_enable_exhausts_retry_raises_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(
        azure,
        "_ensure_dashboard_mi_resource_group_contributor",
        lambda *_a, **_kw: {
            "roles_assigned": [],
            "roles_failed": {"Contributor": "denied"},
            "mi_principal_id": "mi-oid",
        },
    )
    # Collapse the retry window so the first failure exhausts immediately.
    monkeypatch.setattr(task_mod, "_LINKED_AUTH_RETRY_SECONDS", 0.0)

    def always_linked_auth(*_a, **_kw):
        raise _LinkedAuthError()

    monkeypatch.setattr(task_mod, "enable_container_insights", always_linked_auth)

    with pytest.raises(RuntimeError) as excinfo:
        _run()

    message = str(excinfo.value)
    assert "Microsoft.OperationsManagement/solutions/write" in message
    assert "az role assignment create" in message
    assert "--assignee mi-oid" in message
    assert "defaultresourcegroup-se" in message


def test_non_linked_auth_error_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(
        azure,
        "_ensure_dashboard_mi_resource_group_contributor",
        lambda *_a, **_kw: {"roles_assigned": ["Contributor"], "roles_failed": {}},
    )

    calls = {"n": 0}

    def boom(*_a, **_kw):
        calls["n"] += 1
        raise ValueError("unrelated failure")

    monkeypatch.setattr(task_mod, "enable_container_insights", boom)

    with pytest.raises(ValueError, match="unrelated failure"):
        _run()
    assert calls["n"] == 1


def test_self_grant_failure_does_not_abort_enable(monkeypatch) -> None:
    def raising_grant(*_a, **_kw):
        raise RuntimeError("self-grant blew up")

    monkeypatch.setattr(
        azure, "_ensure_dashboard_mi_resource_group_contributor", raising_grant
    )
    monkeypatch.setattr(
        task_mod,
        "enable_container_insights",
        lambda *_a, **_kw: {"enabled": True},
    )

    state = _run()

    assert state["enabled"] is True


def test_unparseable_workspace_id_skips_grant_but_enables(monkeypatch) -> None:
    grant_calls = {"n": 0}

    def fake_grant(*_a, **_kw):
        grant_calls["n"] += 1
        return {"roles_assigned": [], "roles_failed": {}}

    monkeypatch.setattr(
        azure, "_ensure_dashboard_mi_resource_group_contributor", fake_grant
    )
    monkeypatch.setattr(
        task_mod,
        "enable_container_insights",
        lambda *_a, **_kw: {"enabled": True},
    )

    state = _run(workspace_resource_id="not-an-arm-id")

    assert state["enabled"] is True
    assert grant_calls["n"] == 0
