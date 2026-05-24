"""Tests for `api.services.aks_availability` + the new preflight HTTP route.

Responsibility: Verify SKU availability listing, quota probing, RG access
    checks and the composed `run_provision_preflight` happy/sad paths. Plus
    a smoke test through the `/api/aks/preflight` and `/api/aks/available-skus`
    routes so the FE contract stays stable.
Edit boundaries: Pure unit tests with fake Azure SDK clients; no live
    network or credentials.
Key entry points: see the per-test docstrings.
Risky contracts: When `azure.mgmt.compute` shapes change (`Restrictions`,
    `Usage`, etc.) these tests are the canary.
Validation: `uv run pytest -q api/tests/test_aks_availability.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


class _Restriction:
    def __init__(self, reason_code: str, rtype: str = "Location") -> None:
        self.reason_code = reason_code
        self.type = rtype


class _Sku:
    def __init__(
        self,
        name: str,
        resource_type: str = "virtualMachines",
        locations: tuple[str, ...] = ("koreacentral",),
        restrictions: list[_Restriction] | None = None,
    ) -> None:
        self.name = name
        self.resource_type = resource_type
        self.locations = list(locations)
        self.restrictions = restrictions or []


class _UsageName:
    def __init__(self, value: str) -> None:
        self.value = value


class _Usage:
    def __init__(self, name: str, current: int, limit: int) -> None:
        self.name = _UsageName(name)
        self.current_value = current
        self.limit = limit


class _ResourceSkus:
    def __init__(self, skus: list[_Sku]) -> None:
        self._skus = skus

    def list(self, filter: str | None = None) -> list[_Sku]:
        return list(self._skus)


class _UsageOp:
    def __init__(self, usages: list[_Usage]) -> None:
        self._usages = usages

    def list(self, _region: str) -> list[_Usage]:
        return list(self._usages)


class _FakeComputeClient:
    def __init__(self, skus: list[_Sku], usages: list[_Usage]) -> None:
        self.resource_skus = _ResourceSkus(skus)
        self.usage = _UsageOp(usages)


def _patch_compute(monkeypatch: pytest.MonkeyPatch, client: _FakeComputeClient) -> None:
    import api.services.aks_availability as availability

    monkeypatch.setattr(availability, "compute_client", lambda _c, _s: client)


def test_list_region_sku_availability_marks_blocked_skus_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NotAvailableForSubscription` from Azure must surface as
    `available=False` with the reason preserved."""
    import api.services.aks_availability as availability

    skus = [
        _Sku("Standard_E16s_v5", restrictions=[_Restriction("NotAvailableForSubscription")]),
        _Sku("Standard_D2s_v3"),  # no restrictions → available
        _Sku("Standard_NOT_IN_ALLOWLIST"),  # ignored — not in our allow list
    ]
    _patch_compute(monkeypatch, _FakeComputeClient(skus, []))

    result = availability.list_region_sku_availability(object(), "sub-1", "koreacentral")

    assert result["Standard_E16s_v5"].available is False
    assert result["Standard_E16s_v5"].reason == "NotAvailableForSubscription"
    assert result["Standard_E16s_v5"].location_restricted is True
    assert result["Standard_D2s_v3"].available is True
    # SKUs that Azure didn't return at all become UnknownToAzure.
    if "Standard_E64s_v5" in result:
        assert result["Standard_E64s_v5"].available is False
        assert result["Standard_E64s_v5"].reason == "UnknownToAzure"


def test_check_compute_quota_flags_family_shortfall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a family bucket lacks headroom for the requested cores the
    QuotaCheck must surface `ok=False` with a helpful message."""
    import api.services.aks_availability as availability

    # Standard_E16s_v5 has 16 vCPUs; 10 nodes = 160. ESv5 family has
    # current=100, limit=160 → headroom 60 < needed 160.
    usages = [
        _Usage("standardESv5Family", current=100, limit=160),
        _Usage("standardDSv3Family", current=0, limit=200),
        _Usage("cores", current=100, limit=500),
    ]
    _patch_compute(monkeypatch, _FakeComputeClient([], usages))

    checks = availability.check_compute_quota(
        object(),
        "sub-1",
        "koreacentral",
        {"Standard_E16s_v5": 10, "Standard_D2s_v3": 1},
    )

    by_family = {c.family: c for c in checks}
    assert by_family["standardESv5Family"].ok is False
    assert by_family["standardESv5Family"].needed == 160
    assert by_family["standardDSv3Family"].ok is True
    # The Total Regional row must also be present.
    assert "Total Regional vCPUs" in by_family


def test_check_resource_group_access_reports_existing_rg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing RG must report `exists=True` plus a location_match flag."""
    import api.services.aks_availability as availability

    class _Rg:
        location = "koreacentral"

    class _Groups:
        def get(self, _name: str) -> _Rg:
            return _Rg()

    class _Rc:
        resource_groups = _Groups()

    monkeypatch.setattr(availability, "resource_client", lambda _c, _s: _Rc())

    res = availability.check_resource_group_access(object(), "sub", "rg-x", "koreacentral")
    assert res.exists is True
    assert res.location_match is True


def test_run_provision_preflight_fails_when_sku_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when either pool SKU is blocked, overall `ok` must be
    False and the `skus` row must carry `status='fail'` plus the
    unavailable SKU details. The quota/RG rows still render."""
    import api.services.aks_availability as availability

    skus = [
        _Sku("Standard_E16s_v5", restrictions=[_Restriction("NotAvailableForSubscription")]),
        _Sku("Standard_D2s_v3"),
    ]
    usages = [
        _Usage("standardESv5Family", 0, 1000),
        _Usage("standardDSv3Family", 0, 1000),
        _Usage("cores", 0, 1000),
    ]
    _patch_compute(monkeypatch, _FakeComputeClient(skus, usages))

    class _Rc:
        class resource_groups:
            @staticmethod
            def get(_name: str) -> Any:
                from azure.core.exceptions import ResourceNotFoundError

                raise ResourceNotFoundError(message="missing")

    monkeypatch.setattr(availability, "resource_client", lambda _c, _s: _Rc())

    ok, checks = availability.run_provision_preflight(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb-cluster",
        region="koreacentral",
        node_sku="Standard_E16s_v5",
        node_count=10,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
    )

    assert ok is False
    by_name = {c.name: c for c in checks}
    assert by_name["skus"].status == "fail"
    assert by_name["quota"].status == "ok"
    assert by_name["resource_group"].status == "ok"
    assert "Standard_E16s_v5" in by_name["skus"].message


def test_azure_portal_aks_url_builds_a_clean_deep_link() -> None:
    from api.services.aks_availability import azure_portal_aks_url

    url = azure_portal_aks_url("sub-1", "rg-x", "elb-cluster-01")
    assert url.startswith("https://portal.azure.com/#@/resource/subscriptions/sub-1/")
    assert "elb-cluster-01" in url
    assert url.endswith("/overview")


def test_run_provision_preflight_fails_when_quota_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota shortfall must be `fail` (not `warn`) — it's a deterministic
    ARM-enforced limit that re-trying with the same payload always
    rejects. The details must carry `max_blast_nodes_fit` so the FE can
    offer an "Apply" suggestion."""
    import api.services.aks_availability as availability

    # Standard_E16s_v5 = 16 vCPU; 10 nodes = 160; plus 1 × D2s_v3 = 2; total 162.
    # Total Regional vCPUs quota: limit=100, current=0 → 100 free → way short.
    skus = [
        _Sku("Standard_E16s_v5"),
        _Sku("Standard_D2s_v3"),
    ]
    usages = [
        _Usage("standardESv5Family", 0, 1000),
        _Usage("standardDSv3Family", 0, 1000),
        _Usage("cores", 0, 100),  # the binding constraint
    ]
    _patch_compute(monkeypatch, _FakeComputeClient(skus, usages))

    class _Rc:
        class resource_groups:
            @staticmethod
            def get(_name: str) -> Any:
                class _Rg:
                    location = "koreacentral"

                return _Rg()

    monkeypatch.setattr(availability, "resource_client", lambda _c, _s: _Rc())

    ok, checks = availability.run_provision_preflight(
        object(),
        subscription_id="sub-1",
        resource_group="rg-x",
        region="koreacentral",
        node_sku="Standard_E16s_v5",
        node_count=10,
        system_vm_size="Standard_D2s_v3",
        system_node_count=1,
    )

    assert ok is False
    quota = next(c for c in checks if c.name == "quota")
    assert quota.status == "fail"
    # Total regional is the binding constraint: 100 free, system_pool eats 2,
    # so 98 cores left for the blast pool → 98 / 16 = 6 nodes max.
    assert quota.details["binding_family"] == "Total Regional vCPUs"
    assert quota.details["max_blast_nodes_fit"] == 6
    assert quota.details["blast_cores_per_node"] == 16
    assert quota.details["system_cores_total"] == 2


# ----- Route smoke tests (TestClient) ------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def test_route_available_skus_returns_filtered_lists(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `/api/aks/available-skus` route must return both buckets and
    the reason for any unavailable SKU."""
    import api.routes.aks.preflight as preflight_mod
    import api.services.aks_availability as availability

    skus = [
        _Sku("Standard_E16s_v5", restrictions=[_Restriction("NotAvailableForSubscription")]),
        _Sku("Standard_D2s_v3"),
    ]
    _patch_compute(monkeypatch, _FakeComputeClient(skus, []))
    monkeypatch.setattr(preflight_mod, "get_credential", lambda: object())
    monkeypatch.setattr(availability, "compute_client", lambda _c, _s: _FakeComputeClient(skus, []))

    resp = client.get(
        "/api/aks/available-skus",
        params={"subscription_id": "sub-1", "region": "koreacentral"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "Standard_D2s_v3" in body["available"]
    blocked = [u for u in body["unavailable"] if u["name"] == "Standard_E16s_v5"]
    assert blocked, body
    assert blocked[0]["reason"] == "NotAvailableForSubscription"


def test_route_preflight_returns_fail_when_sku_blocked(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/api/aks/preflight` returns `ok=false` + a structured `checks[]`
    when any SKU is not available."""
    import api.routes.aks.preflight as preflight_mod
    import api.services.aks_availability as availability

    skus = [
        _Sku("Standard_E16s_v5", restrictions=[_Restriction("NotAvailableForSubscription")]),
        _Sku("Standard_D2s_v3"),
    ]
    fake = _FakeComputeClient(skus, [])
    _patch_compute(monkeypatch, fake)
    monkeypatch.setattr(preflight_mod, "get_credential", lambda: object())
    monkeypatch.setattr(availability, "compute_client", lambda _c, _s: fake)

    class _Rc:
        class resource_groups:
            @staticmethod
            def get(_name: str) -> Any:
                class _Rg:
                    location = "koreacentral"

                return _Rg()

    monkeypatch.setattr(availability, "resource_client", lambda _c, _s: _Rc())

    resp = client.post(
        "/api/aks/preflight",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "region": "koreacentral",
            "cluster_name": "elb-cluster-01",
            "node_sku": "Standard_E16s_v5",
            "node_count": 10,
            "system_vm_size": "Standard_D2s_v3",
            "system_node_count": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    rows = {c["name"]: c for c in body["checks"]}
    assert rows["skus"]["status"] == "fail"
    assert "Standard_E16s_v5" in rows["skus"]["message"]
    assert body["portal_url"].startswith("https://portal.azure.com/")
