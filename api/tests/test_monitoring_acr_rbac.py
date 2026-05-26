"""ACR provisioning RBAC tests.

Mirrors `test_monitoring_storage_rbac.py` for the ACR onboarding path.

Responsibility: Cover role assignment side effects for ACR onboarding (the SPA
"Create new ACR" wizard).
Edit boundaries: Keep these tests focused on api.services.monitoring.ensure_acr
RBAC behaviour; broader Azure SDK integration belongs in smoke tests.
Key entry points:
- `test_ensure_acr_assigns_pull_to_caller_and_trio_to_uami`
- `test_ensure_acr_skips_uami_assignment_without_principal_env`
- `test_ensure_acr_grants_correct_acrpull_guid_not_legacy_acrpush`
Risky contracts: The api/worker sidecars pull images, push images, and run
ACR Tasks `scheduleRun/action` through the shared UAMI \u2014 not the browser
caller \u2014 so ACR onboarding must grant that UAMI AcrPull + AcrPush +
Contributor (mirrors `infra/modules/acr.bicep` for the platform ACR).
Validation: `uv run pytest -q api/tests/test_monitoring_acr_rbac.py`.
"""

from __future__ import annotations

import pytest
from api.services import monitoring


class _Registries:
    def begin_create(self, *_args, **_kwargs) -> object:
        class _Poller:
            def result(self) -> None:
                return None

        return _Poller()


class _AcrClient:
    def __init__(self) -> None:
        self.registries = _Registries()


def _record_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []

    def fake_assign(
        credential: object,
        subscription_id: str,
        principal_id: str,
        scope: str,
        role_definition_id: str,
        principal_type: str = "User",
    ) -> bool:
        calls.append(
            {
                "subscription_id": subscription_id,
                "principal_id": principal_id,
                "scope": scope,
                "role_definition_id": role_definition_id,
                "principal_type": principal_type,
            }
        )
        return True

    monkeypatch.setattr(monitoring, "acr_client", lambda *_args: _AcrClient())
    monkeypatch.setattr(monitoring, "_auto_assign_role", fake_assign)
    return calls


def test_ensure_acr_assigns_pull_to_caller_and_trio_to_uami(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SPA wizard path must grant the caller AcrPull and the shared MI
    the same AcrPull + AcrPush + Contributor trio it gets on the platform
    ACR. Without all three the worker cannot pull, push, or schedule ACR
    Tasks against a wizard-attached ACR."""

    calls = _record_calls(monkeypatch)
    monkeypatch.setenv("SHARED_IDENTITY_PRINCIPAL_ID", "uami-principal-id")

    monitoring.ensure_acr(
        object(),
        "sub-123",
        "rg-elb-dashboard",
        "acrelbdashboard",
        "koreacentral",
        caller_oid="caller-object-id",
    )

    scope = (
        "/subscriptions/sub-123/resourceGroups/rg-elb-dashboard"
        "/providers/Microsoft.ContainerRegistry/registries/acrelbdashboard"
    )
    assert calls == [
        {
            "subscription_id": "sub-123",
            "principal_id": "caller-object-id",
            "scope": scope,
            "role_definition_id": monitoring.ACR_PULL_ROLE_ID,
            "principal_type": "User",
        },
        {
            "subscription_id": "sub-123",
            "principal_id": "uami-principal-id",
            "scope": scope,
            "role_definition_id": monitoring.ACR_PULL_ROLE_ID,
            "principal_type": "ServicePrincipal",
        },
        {
            "subscription_id": "sub-123",
            "principal_id": "uami-principal-id",
            "scope": scope,
            "role_definition_id": monitoring.ACR_PUSH_ROLE_ID,
            "principal_type": "ServicePrincipal",
        },
        {
            "subscription_id": "sub-123",
            "principal_id": "uami-principal-id",
            "scope": scope,
            "role_definition_id": monitoring.ACR_CONTRIBUTOR_ROLE_ID,
            "principal_type": "ServicePrincipal",
        },
    ]


def test_ensure_acr_skips_uami_assignment_without_principal_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SHARED_IDENTITY_PRINCIPAL_ID is not set (local dev with no MI
    injected), only the caller assignment fires \u2014 we never invent an MI."""

    calls = _record_calls(monkeypatch)
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)

    monitoring.ensure_acr(
        object(),
        "sub-123",
        "rg-elb-dashboard",
        "acrelbdashboard",
        "koreacentral",
        caller_oid="caller-object-id",
    )

    assert len(calls) == 1
    assert calls[0]["principal_id"] == "caller-object-id"
    assert calls[0]["role_definition_id"] == monitoring.ACR_PULL_ROLE_ID


def test_ensure_acr_grants_correct_acrpull_guid_not_legacy_acrpush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the legacy bug where ACR_PULL_ROLE_ID was
    mis-defined as the AcrPush GUID (8311e382\u2026) and ensure_acr therefore
    granted the caller AcrPush instead of AcrPull, plus never granted the
    MI any ACR role. Pin the canonical Azure built-in role GUIDs against
    the package exports so a future re-typo cannot reintroduce it.

    Reference (Microsoft Learn, Azure built-in roles):
    - AcrPull         7f951dda-4ed3-4680-a7ca-43fe172d538d
    - AcrPush         8311e382-0749-4cb8-b61a-304f252e45ec
    - Contributor     b24988ac-6180-42a0-ab88-20f7382dd24c
    """

    assert monitoring.ACR_PULL_ROLE_ID == "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    assert monitoring.ACR_PUSH_ROLE_ID == "8311e382-0749-4cb8-b61a-304f252e45ec"
    assert monitoring.ACR_CONTRIBUTOR_ROLE_ID == "b24988ac-6180-42a0-ab88-20f7382dd24c"
    assert monitoring.ACR_PULL_ROLE_ID != monitoring.ACR_PUSH_ROLE_ID
