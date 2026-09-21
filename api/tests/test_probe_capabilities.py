"""Postprovision capability-probe contract tests.

Responsibility: Verify stale or missing AKS bootstrap role definitions and
    constrained assignments fail the required postprovision probe.
Edit boundaries: Pure fake-client tests; no Azure calls or resource mutation.
Key entry points: `probe_aks_bootstrap_rbac_contract`, `run_probe`.
Risky contracts: Structural RBAC mismatches must return `fail`, not a warning,
    so postprovision cannot report success with an unusable dashboard UAMI.
Validation: `uv run pytest -q api/tests/test_probe_capabilities.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.dev import probe_capabilities as probe

_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
_PRINCIPAL_ID = "11111111-1111-1111-1111-111111111111"
_ROLE_DEFINITION_ID = (
    f"/subscriptions/{_SUBSCRIPTION_ID}/providers/"
    "Microsoft.Authorization/roleDefinitions/22222222-2222-2222-2222-222222222222"
)
_CONTRIBUTOR_ID = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_UAA_ID = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"


def _condition() -> str:
    return (
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {{{_CONTRIBUTOR_ID}, {_UAA_ID}}} AND "
        "@Request[Microsoft.Authorization/roleAssignments:PrincipalType] "
        "StringEqualsIgnoreCase 'ServicePrincipal'))"
    )


class _Permission:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions


class _Definition:
    role_name = "Elb Workload RG Creator"

    def __init__(self, actions: list[str]) -> None:
        self.permissions = [_Permission(actions)]


class _Assignment:
    scope = f"/subscriptions/{_SUBSCRIPTION_ID}"
    role_definition_id = _ROLE_DEFINITION_ID

    def __init__(self, *, condition: str | None, condition_version: str | None) -> None:
        self.condition = condition
        self.condition_version = condition_version


class _AuthorizationClient:
    def __init__(self, assignment: _Assignment, actions: list[str]) -> None:
        class _Assignments:
            @staticmethod
            def list_for_subscription(filter: str | None = None) -> list[_Assignment]:
                return [assignment]

        class _Definitions:
            @staticmethod
            def get_by_id(_role_definition_id: str) -> _Definition:
                return _Definition(actions)

        self.role_assignments = _Assignments()
        self.role_definitions = _Definitions()


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    condition: str | None,
    condition_version: str | None,
    actions: list[str] | None = None,
) -> None:
    import azure.mgmt.authorization as authorization

    client = _AuthorizationClient(
        _Assignment(condition=condition, condition_version=condition_version),
        actions
        or [
            "Microsoft.Resources/subscriptions/resourceGroups/write",
            "Microsoft.Authorization/roleAssignments/read",
            "Microsoft.Authorization/roleAssignments/write",
        ],
    )
    monkeypatch.setattr(
        authorization,
        "AuthorizationManagementClient",
        lambda _credential, _subscription_id: client,
    )
    monkeypatch.setattr(probe, "_credential", lambda: object())
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", _SUBSCRIPTION_ID)
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", _PRINCIPAL_ID)


def test_aks_bootstrap_contract_probe_accepts_current_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        condition=_condition(),
        condition_version="2.0",
    )

    result = probe.probe_aks_bootstrap_rbac_contract()

    assert result == "Elb Workload RG Creator definition + assignment OK"


def test_aks_bootstrap_contract_probe_rejects_missing_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, condition=None, condition_version=None)

    with pytest.raises(probe.RequiredCapabilityMissing, match="conditionVersion"):
        probe.probe_aks_bootstrap_rbac_contract()


def test_required_contract_mismatch_is_a_failed_probe(capsys: Any) -> None:
    def _missing() -> str:
        raise probe.RequiredCapabilityMissing("roleAssignments/write missing")

    result = probe.run_probe(
        probe.Probe(
            name="AKS bootstrap RBAC contract",
            runner=_missing,
            role="Elb Workload RG Creator",
            bicep="infra/modules/workloadRgCreatorRole.bicep",
        )
    )

    assert result == "fail"
    assert "roleAssignments/write missing" in capsys.readouterr().out
