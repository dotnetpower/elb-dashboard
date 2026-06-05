"""Tests for `/api/me`.

Responsibility: Lock in the response shape of `/api/me` so the SPA contract stays stable
when the route is extended (subscriptions, errors, …).
Edit boundaries: Pure FastAPI / monkeypatch tests; do not require real Azure access.
Key entry points: `test_me_returns_identity_with_subscriptions`,
`test_me_surfaces_subscriptions_error_field`.
Risky contracts: SPA branches on `subscriptions` and `subscriptions_error`. Renaming either
field requires a coordinated SPA change.
Validation: `uv run pytest -q api/tests/test_me_route.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    from api.main import app

    return TestClient(app)


def test_me_returns_identity_fields(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """Even when the subscription listing fails, identity claims must come through."""
    import api.routes.me as me_module

    monkeypatch.setattr(
        me_module, "_list_visible_subscriptions", lambda: ([], "boom: ARM offline")
    )
    res = client.get("/api/me")
    assert res.status_code == 200
    body = res.json()
    assert {"object_id", "tenant_id", "upn", "subscriptions"} <= body.keys()
    assert body["subscriptions"] == []
    assert "subscriptions_error" in body
    assert "boom" in body["subscriptions_error"]


def test_me_returns_subscriptions(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import api.routes.me as me_module

    fake = [
        {
            "subscriptionId": "11111111-1111-1111-1111-111111111111",
            "displayName": "Demo One",
            "tenantId": "tenant-a",
            "state": "Enabled",
        },
        {
            "subscriptionId": "22222222-2222-2222-2222-222222222222",
            "displayName": "Other",
            "tenantId": "tenant-b",
            "state": "Enabled",
        },
    ]
    monkeypatch.setattr(me_module, "_list_visible_subscriptions", lambda: (fake, None))

    res = client.get("/api/me")
    assert res.status_code == 200
    body = res.json()
    assert body["subscriptions"] == fake
    assert "subscriptions_error" not in body


def test_list_visible_subscriptions_uses_short_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.routes.me as me_module

    calls = 0

    class _FakeState:
        value = "Enabled"

    class _FakeSub:
        subscription_id = "sub-1"
        display_name = "Demo"
        tenant_id = "tenant-1"
        state = _FakeState()

    class _FakeSubscriptions:
        def list(self):
            nonlocal calls
            calls += 1
            return [_FakeSub()]

    class _FakeClient:
        def __init__(self, _credential: object) -> None:
            self.subscriptions = _FakeSubscriptions()

    me_module.reset_subscription_cache_for_tests()
    monkeypatch.setattr(me_module, "get_credential", lambda: object())
    monkeypatch.setattr("azure.mgmt.resource.SubscriptionClient", _FakeClient, raising=True)

    first, first_error = me_module._list_visible_subscriptions()
    second, second_error = me_module._list_visible_subscriptions()

    assert first_error is None
    assert second_error is None
    assert first == second
    assert calls == 1


def test_me_requires_caller(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without AUTH_DEV_BYPASS, anonymous requests must be rejected.

    `require_caller` reads `AUTH_DEV_BYPASS` lazily on every call, so simply
    flipping the env var is enough — no module reload required. The previous
    importlib.reload approach was brittle (any other test that had already
    imported `api.main` could see torn-down state).
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "false")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    res = client.get("/api/me")
    assert res.status_code in (401, 403)


def test_me_permissions_returns_capability_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Critique #6: `/api/me/permissions` must return the documented
    capability shape so the SPA can disable buttons based on it."""
    from api.services import me_permissions as svc

    svc.reset_permissions_cache_for_tests()
    monkeypatch.setattr(
        svc,
        "_enumerate_role_assignments",
        lambda credential, sub, oid: (
            [
                (
                    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",  # Owner
                    f"/subscriptions/{sub}".lower(),
                )
            ],
            None,
        ),
    )

    res = client.get(
        "/api/me/permissions?subscription_id=SUB&resource_group=rg-elb"
    )
    assert res.status_code == 200
    body = res.json()
    for key in (
        "can_read",
        "can_write",
        "can_start_stop",
        "can_delete",
        "can_submit_blast",
        "can_build_acr",
        "can_grant_rbac",
        "degraded",
        "matched_roles",
        "matched_role_names",
        "reason",
    ):
        assert key in body, f"missing key {key}"
    # Owner at sub scope grants every capability for rg-scoped query.
    assert body["can_write"] is True
    assert body["can_delete"] is True
    assert "Owner" in body["matched_role_names"]


def test_me_permissions_requires_subscription_id(
    client: TestClient,
) -> None:
    res = client.get("/api/me/permissions")
    assert res.status_code == 422  # FastAPI Query(...) required validation


def test_me_access_review_groups_per_resource_group(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`/api/me/access-review` returns one group per requested RG with
    inheritance metadata so the SPA can render an IAM-style table."""
    from api.services import access_review as svc

    svc.reset_access_review_cache_for_tests()
    monkeypatch.setattr(
        svc,
        "_enumerate",
        lambda credential, sub, oid: (
            [
                (
                    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",  # Owner
                    f"/subscriptions/{sub}".lower(),
                    (
                        f"/subscriptions/{sub}/providers/Microsoft.Authorization/"
                        "roleDefinitions/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
                    ).lower(),
                )
            ],
            None,
        ),
    )

    res = client.get(
        "/api/me/access-review?subscription_id=SUB"
        "&resource_group=rg-dashboard&resource_group=rg-cluster"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["subscription_id"] == "SUB"
    assert body["principal"]["kind"] == "user"
    assert body["principal"]["available"] is True
    groups = {g["resource_group"]: g for g in body["groups"]}
    assert set(groups) == {"rg-dashboard", "rg-cluster"}
    row = groups["rg-dashboard"]["assignments"][0]
    assert row["role_name"] == "Owner"
    assert row["inherited"] is True
    assert row["scope_level"] == "subscription"


def test_me_access_review_dashboard_target_uses_mi_principal(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`target=dashboard` reviews the shared managed identity's access, using
    SHARED_IDENTITY_PRINCIPAL_ID as the principal."""
    from api.services import access_review as svc

    svc.reset_access_review_cache_for_tests()
    monkeypatch.setenv(
        "SHARED_IDENTITY_PRINCIPAL_ID",
        "99999999-8888-7777-6666-555555555555",
    )
    seen: dict[str, str] = {}

    def _fake_enumerate(credential, sub, oid):  # type: ignore[no-untyped-def]
        seen["oid"] = oid
        return (
            [
                (
                    "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor
                    f"/subscriptions/{sub}/resourcegroups/rg-cluster".lower(),
                    (
                        f"/subscriptions/{sub}/providers/Microsoft.Authorization/"
                        "roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"
                    ).lower(),
                )
            ],
            None,
        )

    monkeypatch.setattr(svc, "_enumerate", _fake_enumerate)

    res = client.get(
        "/api/me/access-review?subscription_id=SUB"
        "&resource_group=rg-cluster&target=dashboard"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["principal"]["kind"] == "dashboard_identity"
    assert body["principal"]["object_id"] == "99999999-8888-7777-6666-555555555555"
    assert body["principal"]["available"] is True
    assert seen["oid"] == "99999999-8888-7777-6666-555555555555"
    assert body["groups"][0]["assignments"][0]["role_name"] == "Contributor"


def test_me_access_review_dashboard_unavailable_without_mi(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`target=dashboard` in a shell with no MI principal id reports the
    identity as unavailable rather than a misleading empty access table."""
    from api.services import access_review as svc

    svc.reset_access_review_cache_for_tests()
    monkeypatch.delenv("SHARED_IDENTITY_PRINCIPAL_ID", raising=False)

    res = client.get(
        "/api/me/access-review?subscription_id=SUB"
        "&resource_group=rg-cluster&target=dashboard"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["principal"]["kind"] == "dashboard_identity"
    assert body["principal"]["available"] is False
    assert body["groups"] == []


def test_me_access_review_requires_subscription_id(client: TestClient) -> None:
    res = client.get("/api/me/access-review?resource_group=rg-elb")
    assert res.status_code == 422

