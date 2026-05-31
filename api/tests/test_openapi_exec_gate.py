"""Tests for the opt-in OpenAPI proxy RBAC execution gate.

Responsibility: Verify ``evaluate_openapi_exec_gate`` enforces the
    write-role requirement ONLY when ``ENFORCE_OPENAPI_EXEC_RBAC`` is set
    (charter §12a Rule 4: positive ON path AND legacy OFF path), and that
    it fails closed when the RBAC lookup is indeterminate.
Edit boundaries: No network / ARM. Patch ``compute_caller_permissions``
    with a fake; drive enforcement via monkeypatched env.
Key entry points: ``test_*``.
Risky contracts: The gate MUST deny on ``degraded=True`` (fail-closed)
    even though the underlying UX helper degrades open.
Validation: ``uv run pytest -q api/tests/test_openapi_exec_gate.py``.
"""

from __future__ import annotations

import pytest
from api.auth import DEV_BYPASS_OID, CallerIdentity
from api.services.me_permissions import CallerPermissions
from api.services.openapi import exec_gate


def _caller(oid: str = "44444444-4444-4444-4444-444444444444") -> CallerIdentity:
    return CallerIdentity(
        object_id=oid,
        tenant_id="22222222-2222-2222-2222-222222222222",
        upn="user1@example.test",
        raw_token="synthetic",
        claims={"roles": []},
    )


def _perms(
    *, can_write: bool, degraded: bool = False, names: tuple[str, ...] = ()
) -> CallerPermissions:
    return CallerPermissions(
        can_read=True,
        can_write=can_write,
        can_start_stop=can_write,
        can_delete=False,
        can_submit_blast=can_write,
        can_build_acr=can_write,
        can_grant_rbac=False,
        degraded=degraded,
        matched_roles=(),
        matched_role_names=names,
        reason="" if can_write else "no_role_at_scope",
    )


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OPENAPI_EXEC_RBAC", "true")


def _patch_perms(monkeypatch: pytest.MonkeyPatch, perms: CallerPermissions) -> list[dict]:
    calls: list[dict] = []

    def _fake(credential, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return perms

    monkeypatch.setattr(exec_gate, "compute_caller_permissions", _fake)
    return calls


# --- Legacy OFF path (charter §12a Rule 4 — default preserves behaviour) ---


def test_disabled_allows_any_state_changing_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENFORCE_OPENAPI_EXEC_RBAC", raising=False)
    # Even a no-write caller is allowed when enforcement is off.
    decision = exec_gate.evaluate_openapi_exec_gate(
        object(),
        caller=_caller(),
        method="POST",
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
    )
    assert decision.allowed is True
    assert decision.reason == "enforcement_disabled"


def test_is_exec_rbac_enforced_truthy_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for token in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("ENFORCE_OPENAPI_EXEC_RBAC", token)
        assert exec_gate.is_exec_rbac_enforced() is True
    for token in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("ENFORCE_OPENAPI_EXEC_RBAC", token)
        assert exec_gate.is_exec_rbac_enforced() is False


# --- Enforced ON path ---


def test_enforced_read_only_method_is_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    calls = _patch_perms(monkeypatch, _perms(can_write=False))
    decision = exec_gate.evaluate_openapi_exec_gate(
        object(),
        caller=_caller(),
        method="GET",
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
    )
    assert decision.allowed is True
    assert decision.reason == "read_only_method"
    # RBAC lookup must be skipped for read-only methods.
    assert calls == []


def test_enforced_dev_bypass_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    calls = _patch_perms(monkeypatch, _perms(can_write=False))
    decision = exec_gate.evaluate_openapi_exec_gate(
        object(),
        caller=_caller(oid=DEV_BYPASS_OID),
        method="POST",
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
    )
    assert decision.allowed is True
    assert decision.reason == "dev_bypass"
    assert calls == []


def test_enforced_write_role_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _patch_perms(monkeypatch, _perms(can_write=True, names=("Contributor",)))
    decision = exec_gate.evaluate_openapi_exec_gate(
        object(),
        caller=_caller(),
        method="POST",
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
    )
    assert decision.allowed is True
    assert decision.reason == "rbac_write_ok"


def test_enforced_no_write_role_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _patch_perms(monkeypatch, _perms(can_write=False, names=("Reader",)))
    decision = exec_gate.evaluate_openapi_exec_gate(
        object(),
        caller=_caller(),
        method="DELETE",
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
    )
    assert decision.allowed is False
    assert decision.status_code == 403
    assert decision.detail["code"] == "openapi_exec_forbidden"
    assert decision.detail["matched_roles"] == ["Reader"]
    assert "rg-elb-dashboard" in decision.detail["message"]


def test_enforced_degraded_lookup_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    # Even though the UX helper opened all caps, degraded=True must DENY.
    _patch_perms(monkeypatch, _perms(can_write=True, degraded=True))
    decision = exec_gate.evaluate_openapi_exec_gate(
        object(),
        caller=_caller(),
        method="POST",
        subscription_id="sub-1",
        resource_group="rg-elb-dashboard",
    )
    assert decision.allowed is False
    assert decision.status_code == 403
    assert decision.detail["code"] == "openapi_exec_rbac_indeterminate"
