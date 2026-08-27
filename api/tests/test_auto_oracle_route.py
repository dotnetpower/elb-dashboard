"""Tests for the authenticated Auto oracle preference API.

Responsibility: Verify Auto warm dependency enforcement, caller ownership
    stamping, disabled-save behavior, and scope-filtered listing.
Edit boundaries: HTTP layer with service mocks only; no Azure or local files.
Key entry points: `test_enable_requires_auto_warm`,
    `test_enabled_preference_is_saved_for_caller`,
    `test_disabled_preference_does_not_require_auto_warm`.
Risky contracts: Enabling automation must never bypass Auto warm readiness and
    every persisted row must carry the authenticated caller identity.
Validation: `uv run pytest -q api/tests/test_auto_oracle_route.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services.auto_oracle import AutoOraclePreference
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _body(**overrides: object) -> dict[str, object]:
    return {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "cluster_resource_group": "rg-aks",
        "cluster_name": "aks-1",
        "storage_resource_group": "rg-storage",
        "storage_account": "stelbtest",
        "db_name": "core_nt",
        "acr_name": "acrelb",
        "enabled": True,
        **overrides,
    }


def test_enable_requires_auto_warm(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args, **_kwargs: None,
    )

    response = client.put("/api/warmup/oracle-preference", json=_body())

    assert response.status_code == 409
    assert response.json()["code"] == "auto_warm_required"


def test_preference_request_rejects_unknown_fields(client: TestClient) -> None:
    response = client.put(
        "/api/warmup/oracle-preference",
        json=_body(cluster_resorce_group="typo"),
    )

    assert response.status_code == 422


def test_enabled_preference_is_saved_for_caller(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = []
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args, **_kwargs: SimpleNamespace(
            enabled=True,
            databases=["core_nt"],
            storage_account="stelbtest",
            storage_resource_group="rg-storage",
        ),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda pref, **_kwargs: saved.append(pref) or pref,
    )

    response = client.put("/api/warmup/oracle-preference", json=_body())

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "saved_inactive"
    assert response.json()["reconcile_task_id"] == ""
    assert saved[0].enabled is True
    assert saved[0].owner_oid == "00000000-0000-0000-0000-000000000000"
    response_preference = response.json()["preference"]
    assert response_preference["cluster_resource_group"] == "rg-aks"
    assert response_preference["cluster_name"] == "aks-1"
    assert response_preference["storage_resource_group"] == "rg-storage"
    assert response_preference["storage_account"] == "stelbtest"
    assert response_preference["db_name"] == "core_nt"
    assert response_preference["acr_name"] == "acrelb"
    assert "owner_oid" not in response_preference
    assert "tenant_id" not in response_preference


def test_disabled_preference_does_not_require_auto_warm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = []
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda pref, **_kwargs: saved.append(pref) or pref,
    )

    response = client.put(
        "/api/warmup/oracle-preference",
        json=_body(enabled=False, acr_name=""),
    )

    assert response.status_code == 200, response.text
    assert saved[0].enabled is False


def test_enabled_preference_enqueues_targeted_reconcile_when_gate_is_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args, **_kwargs: SimpleNamespace(
            enabled=True,
            databases=["core_nt"],
            storage_account="stelbtest",
            storage_resource_group="rg-storage",
        ),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda pref, **_kwargs: pref,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_preference",
        lambda *_args: None,
    )
    sent = []
    monkeypatch.setattr(
        "api.celery_app.celery_app.send_task",
        lambda task_name, **kwargs: (
            sent.append((task_name, kwargs)) or SimpleNamespace(id="reconcile-1")
        ),
    )

    response = client.put("/api/warmup/oracle-preference", json=_body())

    assert response.status_code == 200, response.text
    assert response.json()["reconcile_task_id"] == "reconcile-1"
    assert sent[0][0] == "api.tasks.storage.reconcile_auto_oracle"
    assert sent[0][1]["queue"] == "reconcile"


def test_retry_reset_is_authorized_and_persisted_before_enqueue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args, **_kwargs: SimpleNamespace(
            enabled=True,
            databases=["core_nt"],
            storage_account="stelbtest",
            storage_resource_group="rg-storage",
        ),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda pref, **_kwargs: pref,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_preference",
        lambda *_args: None,
    )
    container = object()
    monkeypatch.setattr(
        "api.services.db.oracle_state.oracle_container",
        lambda *_args: container,
    )
    reset = []
    monkeypatch.setattr(
        "api.services.db.oracle_retry.reset_automation_retry",
        lambda value, *, db_name: (
            reset.append((value, db_name)) or {"failure_count": 0, "retry_exhausted": False}
        ),
    )
    monkeypatch.setattr(
        "api.celery_app.celery_app.send_task",
        lambda *_args, **_kwargs: SimpleNamespace(id="retry-reconcile"),
    )

    response = client.put(
        "/api/warmup/oracle-preference",
        json=_body(reset_retry=True),
    )

    assert response.status_code == 200, response.text
    assert reset == [(container, "core_nt")]
    assert response.json()["reconcile_task_id"] == "retry-reconcile"


def test_guard_on_rejects_stale_preference_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args: SimpleNamespace(
            enabled=True,
            databases=["core_nt"],
            storage_account="stelbtest",
            storage_resource_group="rg-storage",
        ),
    )
    existing = AutoOraclePreference(
        subscription_id="00000000-0000-0000-0000-000000000001",
        cluster_resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-storage",
        storage_account="stelbtest",
        db_name="core_nt",
        acr_name="acrelb",
        enabled=True,
        etag="current-version",
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_preference",
        lambda *_args: existing,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda *_args, **_kwargs: pytest.fail("stale version must not write"),
    )

    response = client.put(
        "/api/warmup/oracle-preference",
        json=_body(version="stale-version"),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "auto_oracle_preference_conflict"


def test_guard_on_allows_authorized_modifier_transfer_with_fresh_version(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    existing = AutoOraclePreference(
        subscription_id="00000000-0000-0000-0000-000000000001",
        cluster_resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-storage",
        storage_account="stelbtest",
        db_name="core_nt",
        enabled=False,
        owner_oid="previous-operator",
        etag="current-version",
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_preference",
        lambda *_args: existing,
    )
    saved = []
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda pref, **_kwargs: saved.append(pref) or pref,
    )
    events = []
    monkeypatch.setattr(
        "api.services.feature_events.record_feature_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    response = client.put(
        "/api/warmup/oracle-preference",
        json=_body(enabled=False, acr_name="", version="current-version"),
    )

    assert response.status_code == 200, response.text
    assert saved[0].owner_oid == "00000000-0000-0000-0000-000000000000"
    assert events[0][0] == "oracle_preference_saved"
    assert events[0][1]["modifier_changed"] is True
    assert response.json()["modifier_changed"] is True


def test_guard_on_denies_preference_write_without_capability(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_owner_authorized",
        lambda *_args: (False, "storage_write_denied"),
    )

    response = client.put(
        "/api/warmup/oracle-preference",
        json=_body(enabled=False, acr_name=""),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "auto_oracle_permission_denied"


def test_saved_preference_reports_targeted_enqueue_failure_without_fake_task_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_ORACLE_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_AUTO_ORACLE_RBAC", "true")
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args: SimpleNamespace(
            enabled=True,
            databases=["core_nt"],
            storage_account="stelbtest",
            storage_resource_group="rg-storage",
        ),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.get_auto_oracle_preference",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.save_auto_oracle_preference",
        lambda pref, **_kwargs: pref,
    )
    monkeypatch.setattr(
        "api.celery_app.celery_app.send_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    response = client.put("/api/warmup/oracle-preference", json=_body())

    assert response.status_code == 200
    assert response.json()["status"] == "saved_no_immediate_enqueue"
    assert response.json()["reconcile_task_id"] == ""


def test_enable_rejects_auto_warm_for_different_storage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.auto_warmup.get_auto_warmup_preference",
        lambda *_args, **_kwargs: SimpleNamespace(
            enabled=True,
            databases=["core_nt"],
            storage_account="stother",
            storage_resource_group="rg-storage",
        ),
    )

    response = client.put("/api/warmup/oracle-preference", json=_body())

    assert response.status_code == 409
    assert response.json()["code"] == "auto_warm_required"


def test_preference_list_requires_scope_read_and_redacts_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preference = AutoOraclePreference(
        subscription_id="00000000-0000-0000-0000-000000000001",
        cluster_resource_group="rg-aks",
        cluster_name="aks-1",
        storage_resource_group="rg-storage",
        storage_account="stelbtest",
        db_name="core_nt",
        enabled=True,
        owner_oid="private-owner",
        tenant_id="private-tenant",
    )
    monkeypatch.setattr(
        "api.services.auto_oracle.list_auto_oracle_preference_page",
        lambda **_kwargs: ([preference], "next-page"),
    )
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_scope_read_authorized",
        lambda *_args, **_kwargs: (True, "authorized"),
    )

    response = client.get(
        "/api/warmup/oracle-preferences",
        params={
            "subscription_id": preference.subscription_id,
            "cluster_resource_group": preference.cluster_resource_group,
            "cluster_name": preference.cluster_name,
            "storage_account": preference.storage_account,
        },
    )

    assert response.status_code == 200, response.text
    value = response.json()["preferences"][0]
    assert value["db_name"] == "core_nt"
    assert "owner_oid" not in value
    assert "tenant_id" not in value
    assert response.json()["next_cursor"] == "next-page"


def test_preference_list_denies_caller_without_cluster_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.auto_oracle_reconcile.auto_oracle_scope_read_authorized",
        lambda *_args, **_kwargs: (False, "cluster_read_denied"),
    )

    response = client.get(
        "/api/warmup/oracle-preferences",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "cluster_resource_group": "rg-aks",
            "cluster_name": "aks-1",
            "storage_account": "stelbtest",
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "auto_oracle_read_denied"
    assert response.json()["message"] == ("Read access to the AKS cluster is required.")
    assert response.json()["request_id"]
