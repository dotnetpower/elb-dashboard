"""Tests for the optional dashboard entry gate (`require_dashboard_access`).

Responsibility: Lock in the default-OFF behaviour and the enforced-ON 403 /
    allow matrix of the entry gate added on top of `require_caller`. Pure
    monkeypatch tests; no real Azure access.
Edit boundaries: Gate decision logic lives in
    `api.services.dashboard_access`; this file only asserts its contract and
    the `/api/me` wiring.
Key entry points: `test_*`.
Risky contracts: The gate must (a) preserve legacy behaviour when OFF,
    (b) degrade OPEN when enumeration fails or the platform scope is unset,
    (c) 403 a no-role caller only when enforcement is ON and enumeration
    succeeds. The dev-bypass identity must always pass.
Validation: `uv run pytest -q api/tests/test_dashboard_access.py`.
"""

from __future__ import annotations

import pytest
from api.auth import DEV_BYPASS_OID, CallerIdentity, require_caller
from api.services import dashboard_access
from api.services.dashboard_access import (
    DASHBOARD_ACCESS_DENIED_CODE,
    has_dashboard_read_access,
    is_dashboard_rbac_enforced,
    require_dashboard_access,
)
from fastapi.testclient import TestClient


def _caller(oid: str = "55555555-5555-5555-5555-555555555555") -> CallerIdentity:
    return CallerIdentity(
        object_id=oid,
        tenant_id="22222222-2222-2222-2222-222222222222",
        upn="user2@example.test",
        raw_token="synthetic",
        claims={},
    )


def _fake_perms(*, can_read: bool, degraded: bool = False) -> object:
    """Minimal stand-in for `CallerPermissions`."""
    return type(
        "_Perms",
        (),
        {"can_read": can_read, "degraded": degraded, "reason": "test"},
    )()


def _patch_perms(monkeypatch: pytest.MonkeyPatch, perms: object) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.me_permissions.compute_caller_permissions",
        lambda *a, **k: perms,
    )


# ---------------------------------------------------------------------------
# is_dashboard_rbac_enforced
# ---------------------------------------------------------------------------


def test_enforcement_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENFORCE_DASHBOARD_RBAC", raising=False)
    assert is_dashboard_rbac_enforced() is False


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_enforcement_on_when_true(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ENFORCE_DASHBOARD_RBAC", value)
    assert is_dashboard_rbac_enforced() is True


@pytest.mark.parametrize("value", ["false", "0", "", "yes"])
def test_enforcement_off_for_non_true(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ENFORCE_DASHBOARD_RBAC", value)
    assert is_dashboard_rbac_enforced() is False


# ---------------------------------------------------------------------------
# has_dashboard_read_access
# ---------------------------------------------------------------------------


def test_dev_bypass_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    assert has_dashboard_read_access(_caller(DEV_BYPASS_OID)) is True


def test_no_oid_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    assert has_dashboard_read_access(_caller("")) is False


def test_no_platform_scope_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    assert has_dashboard_read_access(_caller()) is True


def test_reader_role_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb-dashboard")
    _patch_perms(monkeypatch, _fake_perms(can_read=True))
    assert has_dashboard_read_access(_caller()) is True


def test_no_role_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb-dashboard")
    _patch_perms(monkeypatch, _fake_perms(can_read=False))
    assert has_dashboard_read_access(_caller()) is False


def test_degraded_enumeration_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """SECURITY/UX: enumeration failure must NOT lock everyone out. The MI
    likely lacks roleAssignments/read; opening is all-or-nothing so it never
    selectively admits a no-role attacker."""
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    _patch_perms(monkeypatch, _fake_perms(can_read=False, degraded=True))
    assert has_dashboard_read_access(_caller()) is True


def test_unexpected_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("arm down")

    monkeypatch.setattr(
        "api.services.me_permissions.compute_caller_permissions", _boom
    )
    assert has_dashboard_read_access(_caller()) is True


# ---------------------------------------------------------------------------
# require_dashboard_access (dependency)
# ---------------------------------------------------------------------------


def test_dependency_passthrough_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENFORCE_DASHBOARD_RBAC", raising=False)
    caller = _caller()
    assert require_dashboard_access(caller) is caller


def test_dependency_allows_reader_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_DASHBOARD_RBAC", "true")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb-dashboard")
    _patch_perms(monkeypatch, _fake_perms(can_read=True))
    caller = _caller()
    assert require_dashboard_access(caller) is caller


def test_dependency_denies_no_role_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_DASHBOARD_RBAC", "true")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb-dashboard")
    _patch_perms(monkeypatch, _fake_perms(can_read=False))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        require_dashboard_access(_caller())
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == DASHBOARD_ACCESS_DENIED_CODE
    assert excinfo.value.detail["resource_group"] == "rg-elb-dashboard"
    assert excinfo.value.detail["subscription_id"] == "sub-1"
    # The message names the concrete scope + role so a blocked caller can
    # forward an actionable access request to an administrator.
    message = excinfo.value.detail["message"]
    assert "rg-elb-dashboard" in message
    assert "sub-1" in message
    assert "Reader" in message


# ---------------------------------------------------------------------------
# /api/me route wiring (gate ON)
# ---------------------------------------------------------------------------


@pytest.fixture()
def route_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    import api.routes.me as me_module
    from api.main import app

    monkeypatch.setattr(me_module, "_list_visible_subscriptions", lambda: ([], None))
    return TestClient(app)


def test_route_403_for_no_role_caller(
    monkeypatch: pytest.MonkeyPatch, route_client: TestClient
) -> None:
    monkeypatch.setenv("ENFORCE_DASHBOARD_RBAC", "true")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb-dashboard")
    _patch_perms(monkeypatch, _fake_perms(can_read=False))

    from api.main import app

    app.dependency_overrides[require_caller] = lambda: _caller()
    try:
        res = route_client.get("/api/me")
    finally:
        app.dependency_overrides.pop(require_caller, None)
    assert res.status_code == 403
    # The global StarletteHTTPException handler flattens a dict detail to the
    # top level of the body (it does NOT wrap it under "detail").
    assert res.json()["code"] == DASHBOARD_ACCESS_DENIED_CODE


def test_route_200_for_reader_caller(
    monkeypatch: pytest.MonkeyPatch, route_client: TestClient
) -> None:
    monkeypatch.setenv("ENFORCE_DASHBOARD_RBAC", "true")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb-dashboard")
    _patch_perms(monkeypatch, _fake_perms(can_read=True))

    from api.main import app

    app.dependency_overrides[require_caller] = lambda: _caller()
    try:
        res = route_client.get("/api/me")
    finally:
        app.dependency_overrides.pop(require_caller, None)
    assert res.status_code == 200
    assert res.json()["upn"] == "user2@example.test"


def test_route_200_passthrough_when_disabled(
    monkeypatch: pytest.MonkeyPatch, route_client: TestClient
) -> None:
    monkeypatch.delenv("ENFORCE_DASHBOARD_RBAC", raising=False)

    from api.main import app

    app.dependency_overrides[require_caller] = lambda: _caller()
    try:
        res = route_client.get("/api/me")
    finally:
        app.dependency_overrides.pop(require_caller, None)
    assert res.status_code == 200


def test_module_imports_clean() -> None:
    """Guard against an accidental heavy import at module load."""
    assert dashboard_access.ENFORCE_DASHBOARD_RBAC_ENV == "ENFORCE_DASHBOARD_RBAC"
