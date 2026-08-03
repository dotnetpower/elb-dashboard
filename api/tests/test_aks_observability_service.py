"""Tests for AKS Container Insights provider preflight helpers.

Responsibility: Verify the read-only Microsoft.OperationsManagement registration
    projection used to guard Container Insights enablement.
Edit boundaries: Resource Management clients are faked; no Azure or AKS calls.
Key entry points: ``test_*`` functions.
Risky contracts: Only an explicitly Registered provider enables mutation. Missing
    permissions and ARM failures fail closed without registering the provider.
Validation: ``uv run pytest -q api/tests/test_aks_observability_service.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services import aks_observability as service
from billiard.exceptions import SoftTimeLimitExceeded


def test_registered_provider_allows_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def _get(namespace: str, **kwargs: object) -> object:
        calls.append((namespace, kwargs))
        return SimpleNamespace(
            namespace=namespace,
            registration_state="Registered",
        )

    providers = SimpleNamespace(
        get=_get
    )
    monkeypatch.setattr(
        service,
        "resource_client",
        lambda _credential, _subscription_id: SimpleNamespace(providers=providers),
    )

    status = service.get_container_insights_provider_status(object(), "sub-1")

    assert status == {
        "provider_namespace": "Microsoft.OperationsManagement",
        "provider_registration_state": "Registered",
        "provider_registered": True,
        "enable_available": True,
        "enable_unavailable_reason": "",
        "provider_status_error": "",
    }
    assert calls == [
        (
            "Microsoft.OperationsManagement",
            {"retry_total": 0, "connection_timeout": 5, "read_timeout": 10},
        )
    ]


def test_unregistered_provider_disables_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = SimpleNamespace(
        get=lambda _namespace, **_kwargs: SimpleNamespace(
            registration_state="NotRegistered"
        )
    )
    monkeypatch.setattr(
        service,
        "resource_client",
        lambda _credential, _subscription_id: SimpleNamespace(providers=providers),
    )

    status = service.get_container_insights_provider_status(object(), "sub-1")

    assert status["provider_registered"] is False
    assert status["enable_available"] is False
    assert status["provider_registration_state"] == "NotRegistered"
    assert status["enable_unavailable_reason"] == "provider_not_registered"


def test_provider_read_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("provider read unavailable")

    monkeypatch.setattr(service, "resource_client", _raise)

    status = service.get_container_insights_provider_status(object(), "sub-1")

    assert status["provider_registered"] is False
    assert status["enable_available"] is False
    assert status["provider_registration_state"] == "Unavailable"
    assert status["enable_unavailable_reason"] == "provider_status_unavailable"
    assert status["provider_status_error"] == "RuntimeError"


def test_provider_read_propagates_celery_soft_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _deadline(*_args: object, **_kwargs: object) -> object:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(service, "resource_client", _deadline)

    with pytest.raises(SoftTimeLimitExceeded):
        service.get_container_insights_provider_status(object(), "sub-1")
