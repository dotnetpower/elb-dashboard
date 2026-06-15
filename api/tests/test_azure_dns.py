"""Tests for the Azure DNS custom-domain record helper.

Responsibility: Verify FQDN normalisation, longest-suffix zone matching, apex vs
    sub-domain record-type selection, idempotent upsert, and the best-effort
    degrade paths (no zone / forbidden / error) of
    ``api.services.azure_dns``.
Edit boundaries: Behaviour tests only; the Azure SDK is faked via monkeypatch.
Key entry points: the ``test_*`` functions.
Risky contracts: the helper must NEVER raise — every Azure fault becomes a
    structured result dict the public-HTTPS pipeline reads to choose auto-vs-manual.
Validation: ``uv run pytest -q api/tests/test_azure_dns.py``.
"""

from __future__ import annotations

import pytest
from api.services import azure_dns
from azure.core.exceptions import HttpResponseError


class _FakeZone:
    def __init__(self, name: str, rg: str) -> None:
        self.name = name
        self.id = (
            f"/subscriptions/sub/resourceGroups/{rg}"
            f"/providers/Microsoft.Network/dnszones/{name}"
        )


class _FakeZones:
    def __init__(self, zones: list[_FakeZone]) -> None:
        self._zones = zones

    def list(self):
        return iter(self._zones)


class _FakeRecordSets:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._raise = raise_exc

    def create_or_update(self, rg, zone, name, record_type, params):
        if self._raise is not None:
            raise self._raise
        self.calls.append((rg, zone, name, record_type, params))
        return {"name": name}


class _FakeDnsClient:
    def __init__(self, zones, record_sets=None) -> None:
        self.zones = _FakeZones(zones)
        self.record_sets = record_sets or _FakeRecordSets()


@pytest.fixture(autouse=True)
def _stub_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(azure_dns, "get_credential", lambda: object())


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _FakeDnsClient) -> None:
    monkeypatch.setattr(azure_dns, "dns_client", lambda _cred, _sub: client)


# --- split_custom_domain ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("api.example.com", "api.example.com"),
        ("  API.Example.com  ", "api.example.com"),
        ("https://api.example.com/", "api.example.com"),
        ("http://api.example.com/path", "api.example.com"),
        ("api.example.com.", "api.example.com"),
        ("", ""),
    ],
)
def test_split_custom_domain(raw: str, expected: str) -> None:
    assert azure_dns.split_custom_domain(raw) == expected


# --- find_zone_for_fqdn ----------------------------------------------------


def test_find_zone_picks_owning_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeDnsClient(
        [_FakeZone("elasticblast.com", "rg-elb-dashboard"), _FakeZone("other.com", "rg-x")]
    )
    _install_client(monkeypatch, client)
    match = azure_dns.find_zone_for_fqdn("sub", "api.elasticblast.com")
    assert match is not None
    assert match.zone_name == "elasticblast.com"
    assert match.resource_group == "rg-elb-dashboard"
    assert match.record_name == "api"


def test_find_zone_prefers_longest_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeDnsClient(
        [_FakeZone("example.com", "rg1"), _FakeZone("b.example.com", "rg2")]
    )
    _install_client(monkeypatch, client)
    match = azure_dns.find_zone_for_fqdn("sub", "a.b.example.com")
    assert match is not None
    assert match.zone_name == "b.example.com"
    assert match.record_name == "a"


def test_find_zone_apex(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeDnsClient([_FakeZone("example.com", "rg1")])
    _install_client(monkeypatch, client)
    match = azure_dns.find_zone_for_fqdn("sub", "example.com")
    assert match is not None
    assert match.record_name == "@"


def test_find_zone_none_when_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeDnsClient([_FakeZone("other.com", "rg1")])
    _install_client(monkeypatch, client)
    assert azure_dns.find_zone_for_fqdn("sub", "api.elasticblast.com") is None


def test_find_zone_none_on_list_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_cred, _sub):
        raise HttpResponseError("nope")

    monkeypatch.setattr(azure_dns, "dns_client", _boom)
    assert azure_dns.find_zone_for_fqdn("sub", "api.elasticblast.com") is None


# --- ensure_public_dns_record ----------------------------------------------


def test_ensure_creates_cname(monkeypatch: pytest.MonkeyPatch) -> None:
    record_sets = _FakeRecordSets()
    client = _FakeDnsClient([_FakeZone("elasticblast.com", "rg-elb-dashboard")], record_sets)
    _install_client(monkeypatch, client)
    result = azure_dns.ensure_public_dns_record(
        subscription_id="sub",
        custom_domain="api.elasticblast.com",
        cloudapp_fqdn="elb-openapi-abc.koreacentral.cloudapp.azure.com",
        lb_ip="20.1.2.3",
    )
    assert result["status"] == azure_dns.STATUS_CREATED
    assert result["record_type"] == "CNAME"
    assert result["record_name"] == "api"
    assert len(record_sets.calls) == 1
    _rg, zone, name, rtype, params = record_sets.calls[0]
    assert zone == "elasticblast.com"
    assert name == "api"
    assert rtype == "CNAME"
    assert params["cname_record"]["cname"] == "elb-openapi-abc.koreacentral.cloudapp.azure.com"


def test_ensure_creates_apex_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    record_sets = _FakeRecordSets()
    client = _FakeDnsClient([_FakeZone("elasticblast.com", "rg-elb-dashboard")], record_sets)
    _install_client(monkeypatch, client)
    result = azure_dns.ensure_public_dns_record(
        subscription_id="sub",
        custom_domain="elasticblast.com",
        cloudapp_fqdn="elb-openapi-abc.koreacentral.cloudapp.azure.com",
        lb_ip="20.1.2.3",
    )
    assert result["status"] == azure_dns.STATUS_CREATED
    assert result["record_type"] == "A"
    assert result["record_name"] == "@"
    _rg, _zone, _name, rtype, params = record_sets.calls[0]
    assert rtype == "A"
    assert params["a_records"][0]["ipv4_address"] == "20.1.2.3"


def test_ensure_apex_without_lb_ip_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeDnsClient([_FakeZone("elasticblast.com", "rg-elb-dashboard")])
    _install_client(monkeypatch, client)
    result = azure_dns.ensure_public_dns_record(
        subscription_id="sub",
        custom_domain="elasticblast.com",
        cloudapp_fqdn="elb-openapi-abc.koreacentral.cloudapp.azure.com",
        lb_ip="",
    )
    assert result["status"] == azure_dns.STATUS_ERROR


def test_ensure_no_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeDnsClient([_FakeZone("other.com", "rg1")])
    _install_client(monkeypatch, client)
    result = azure_dns.ensure_public_dns_record(
        subscription_id="sub",
        custom_domain="api.elasticblast.com",
        cloudapp_fqdn="elb-openapi-abc.koreacentral.cloudapp.azure.com",
        lb_ip="20.1.2.3",
    )
    assert result["status"] == azure_dns.STATUS_NO_ZONE
    assert "elb-openapi-abc" in result["detail"]


def test_ensure_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    err = HttpResponseError("forbidden")
    err.status_code = 403
    record_sets = _FakeRecordSets(raise_exc=err)
    client = _FakeDnsClient([_FakeZone("elasticblast.com", "rg-elb-dashboard")], record_sets)
    _install_client(monkeypatch, client)
    result = azure_dns.ensure_public_dns_record(
        subscription_id="sub",
        custom_domain="api.elasticblast.com",
        cloudapp_fqdn="elb-openapi-abc.koreacentral.cloudapp.azure.com",
        lb_ip="20.1.2.3",
    )
    assert result["status"] == azure_dns.STATUS_FORBIDDEN
    assert "DNS Zone Contributor" in result["detail"]


def test_ensure_error_on_other_http_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    err = HttpResponseError("conflict")
    err.status_code = 409
    record_sets = _FakeRecordSets(raise_exc=err)
    client = _FakeDnsClient([_FakeZone("elasticblast.com", "rg-elb-dashboard")], record_sets)
    _install_client(monkeypatch, client)
    result = azure_dns.ensure_public_dns_record(
        subscription_id="sub",
        custom_domain="api.elasticblast.com",
        cloudapp_fqdn="elb-openapi-abc.koreacentral.cloudapp.azure.com",
        lb_ip="20.1.2.3",
    )
    assert result["status"] == azure_dns.STATUS_ERROR
