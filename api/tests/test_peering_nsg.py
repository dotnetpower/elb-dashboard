"""Tests for `api/tasks/azure/peering_nsg.py`.

Responsibility: Cover the four guard properties the SSRF + NSG flow
relies on without crossing the network: (1) target IP must resolve to a
subnet of the target VNet, (2) port allowlist is enforced, (3) source
CIDRs are always pulled from the AKS VNet's address_space, not from
caller input, (4) idempotent re-runs are a no-op and name collisions
with different content refuse to overwrite.
Edit boundaries: Pure unit tests. SDK clients are monkeypatched.
Key entry points: tests under `pytest.mark` defaults.
Risky contracts: The fakes mirror the SDK attribute shapes
(address_space.address_prefixes, subnets[*].address_prefix,
network_security_group.id, security_rules.list/create_or_update). When
SDK fields are renamed upstream, expand the fakes to match — silent
attribute mismatches would let the production guard regress.
Validation: `uv run pytest -q api/tests/test_peering_nsg.py`.
"""

from __future__ import annotations

from typing import Any

import pytest


class _Subnet:
    def __init__(
        self,
        *,
        name: str,
        address_prefix: str,
        nsg_id: str | None = None,
        resource_id: str = "",
    ) -> None:
        self.name = name
        self.address_prefix = address_prefix
        self.address_prefixes: list[str] = []
        self.id = (
            resource_id
            or (
                "/subscriptions/sub-target/resourceGroups/rg-target/"
                f"providers/Microsoft.Network/virtualNetworks/vnet-target/subnets/{name}"
            )
        )

        class _NSGRef:
            def __init__(self, nid: str) -> None:
                self.id = nid

        self.network_security_group = _NSGRef(nsg_id) if nsg_id else None


class _AddrSpace:
    def __init__(self, prefixes: list[str]) -> None:
        self.address_prefixes = prefixes


class _Vnet:
    def __init__(self, *, prefixes: list[str], subnets: list[_Subnet] | None = None) -> None:
        self.address_space = _AddrSpace(prefixes)
        self.subnets = subnets or []


class _FakeVnetsClient:
    def __init__(self, vnet_by_name: dict[str, _Vnet]) -> None:
        self.vnet_by_name = vnet_by_name

    def get(self, _rg: str, name: str, expand: str | None = None) -> _Vnet:
        _ = expand
        return self.vnet_by_name[name]


class _FakeSecurityRule:
    def __init__(
        self,
        *,
        name: str,
        priority: int,
        source_address_prefixes: list[str] | None = None,
        source_address_prefix: str | None = None,
        destination_address_prefix: str | None = None,
        destination_address_prefixes: list[str] | None = None,
        destination_port_ranges: list[str] | None = None,
        destination_port_range: str | None = None,
        access: str = "Allow",
        direction: str = "Inbound",
        protocol: str = "Tcp",
    ) -> None:
        self.name = name
        self.priority = priority
        self.source_address_prefixes = source_address_prefixes or []
        self.source_address_prefix = source_address_prefix
        self.destination_address_prefix = destination_address_prefix
        self.destination_address_prefixes = destination_address_prefixes or []
        self.destination_port_ranges = destination_port_ranges or []
        self.destination_port_range = destination_port_range
        self.access = access
        self.direction = direction
        self.protocol = protocol


class _FakePoller:
    def __init__(self, result: Any = None) -> None:
        self._result = result

    def result(self) -> Any:
        return self._result


class _FakeSecurityRulesClient:
    def __init__(self, initial: list[_FakeSecurityRule]) -> None:
        self.rules: list[_FakeSecurityRule] = list(initial)
        self.created: list[dict[str, Any]] = []

    def list(self, _rg: str, _nsg_name: str) -> list[_FakeSecurityRule]:
        return list(self.rules)

    def begin_create_or_update(
        self, _rg: str, _nsg_name: str, name: str, body: dict[str, Any]
    ) -> _FakePoller:
        self.created.append({"name": name, **body})
        return _FakePoller()


class _FakeNetworkClient:
    def __init__(
        self,
        *,
        vnets: dict[str, _Vnet],
        rules: list[_FakeSecurityRule],
    ) -> None:
        self.virtual_networks = _FakeVnetsClient(vnets)
        self.security_rules = _FakeSecurityRulesClient(rules)


@pytest.fixture
def aks_vnet_id() -> str:
    return (
        "/subscriptions/sub-aks/resourceGroups/MC_rg-aks_clu_kr/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-1"
    )


@pytest.fixture
def target_vnet_id() -> str:
    return (
        "/subscriptions/sub-target/resourceGroups/rg-target/"
        "providers/Microsoft.Network/virtualNetworks/vnet-target"
    )


@pytest.fixture
def aks_vnet() -> _Vnet:
    return _Vnet(prefixes=["10.224.0.0/12"])


@pytest.fixture
def target_vnet() -> _Vnet:
    return _Vnet(
        prefixes=["10.0.0.0/16"],
        subnets=[
            _Subnet(
                name="frontend",
                address_prefix="10.0.1.0/24",
                nsg_id=(
                    "/subscriptions/sub-target/resourceGroups/rg-target/"
                    "providers/Microsoft.Network/networkSecurityGroups/nsg-frontend"
                ),
            ),
            _Subnet(name="backend", address_prefix="10.0.2.0/24"),
        ],
    )


# ---------------------------------------------------------------------------
# resolve_nsg_context
# ---------------------------------------------------------------------------


def test_resolve_nsg_context_finds_subnet_and_nsg(
    monkeypatch: pytest.MonkeyPatch,
    aks_vnet_id: str,
    target_vnet_id: str,
    aks_vnet: _Vnet,
    target_vnet: _Vnet,
) -> None:
    from api.tasks.azure import peering_nsg

    def fake_nc(_cred: Any, sub: str) -> _FakeNetworkClient:
        if sub == "sub-aks":
            return _FakeNetworkClient(vnets={"aks-vnet-1": aks_vnet}, rules=[])
        return _FakeNetworkClient(vnets={"vnet-target": target_vnet}, rules=[])

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    ctx = peering_nsg.resolve_nsg_context(
        object(),
        aks_vnet_id=aks_vnet_id,
        target_vnet_id=target_vnet_id,
        target_ip="10.0.1.50",
    )
    assert ctx is not None
    assert ctx.target_subnet_name == "frontend"
    assert ctx.nsg_name == "nsg-frontend"
    assert ctx.aks_vnet_address_prefixes == ["10.224.0.0/12"]
    assert ctx.target_ip == "10.0.1.50"


def test_resolve_nsg_context_subnet_without_nsg_returns_ctx_with_none(
    monkeypatch: pytest.MonkeyPatch,
    aks_vnet_id: str,
    target_vnet_id: str,
    aks_vnet: _Vnet,
    target_vnet: _Vnet,
) -> None:
    from api.tasks.azure import peering_nsg

    def fake_nc(_cred: Any, sub: str) -> _FakeNetworkClient:
        if sub == "sub-aks":
            return _FakeNetworkClient(vnets={"aks-vnet-1": aks_vnet}, rules=[])
        return _FakeNetworkClient(vnets={"vnet-target": target_vnet}, rules=[])

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    ctx = peering_nsg.resolve_nsg_context(
        object(),
        aks_vnet_id=aks_vnet_id,
        target_vnet_id=target_vnet_id,
        target_ip="10.0.2.99",  # subnet "backend" has no NSG
    )
    assert ctx is not None
    assert ctx.target_subnet_name == "backend"
    assert ctx.nsg_id is None
    assert ctx.nsg_name is None


def test_resolve_nsg_context_ip_outside_any_subnet_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    aks_vnet_id: str,
    target_vnet_id: str,
    aks_vnet: _Vnet,
    target_vnet: _Vnet,
) -> None:
    from api.tasks.azure import peering_nsg

    def fake_nc(_cred: Any, sub: str) -> _FakeNetworkClient:
        if sub == "sub-aks":
            return _FakeNetworkClient(vnets={"aks-vnet-1": aks_vnet}, rules=[])
        return _FakeNetworkClient(vnets={"vnet-target": target_vnet}, rules=[])

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    ctx = peering_nsg.resolve_nsg_context(
        object(),
        aks_vnet_id=aks_vnet_id,
        target_vnet_id=target_vnet_id,
        target_ip="192.168.5.10",  # not in 10.0.0.0/16
    )
    assert ctx is None


# ---------------------------------------------------------------------------
# has_nsg_write_permission
# ---------------------------------------------------------------------------


class _FakePermission:
    def __init__(self, actions: list[str], not_actions: list[str] | None = None) -> None:
        self.actions = actions
        self.not_actions = not_actions or []


class _FakePermsClient:
    def __init__(self, perms: list[_FakePermission]) -> None:
        self._perms = perms

    def list_for_resource(self, **_kwargs: Any) -> list[_FakePermission]:
        return list(self._perms)


def _patch_auth_client(monkeypatch: pytest.MonkeyPatch, perms: list[_FakePermission]) -> None:
    import azure.mgmt.authorization as authmod  # type: ignore[import-not-found]

    class _FakeAuthClient:
        def __init__(self, _cred: Any, _sub: str) -> None:
            self.permissions = _FakePermsClient(perms)

    monkeypatch.setattr(authmod, "AuthorizationManagementClient", _FakeAuthClient)


def test_permission_granted_via_exact_action(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.tasks.azure import peering_nsg

    _patch_auth_client(
        monkeypatch,
        [_FakePermission(["Microsoft.Network/networkSecurityGroups/securityRules/write"])],
    )
    assert peering_nsg.has_nsg_write_permission(
        object(),
        subscription_id="sub-target",
        resource_group="rg-target",
        nsg_name="nsg-frontend",
    )


def test_permission_granted_via_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.tasks.azure import peering_nsg

    _patch_auth_client(monkeypatch, [_FakePermission(["*"])])
    assert peering_nsg.has_nsg_write_permission(
        object(),
        subscription_id="sub-target",
        resource_group="rg-target",
        nsg_name="nsg-frontend",
    )


def test_permission_denied_when_action_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.tasks.azure import peering_nsg

    _patch_auth_client(
        monkeypatch,
        [_FakePermission(["Microsoft.Storage/*/read", "Microsoft.Compute/*"])],
    )
    assert not peering_nsg.has_nsg_write_permission(
        object(),
        subscription_id="sub-target",
        resource_group="rg-target",
        nsg_name="nsg-frontend",
    )


def test_permission_denied_when_not_action_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.tasks.azure import peering_nsg

    _patch_auth_client(
        monkeypatch,
        [
            _FakePermission(
                ["Microsoft.Network/*"],
                not_actions=[
                    "Microsoft.Network/networkSecurityGroups/securityRules/write",
                ],
            )
        ],
    )
    assert not peering_nsg.has_nsg_write_permission(
        object(),
        subscription_id="sub-target",
        resource_group="rg-target",
        nsg_name="nsg-frontend",
    )


def test_permission_check_swallows_arm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network errors must default to ``False`` so a transient ARM failure
    cannot become a permissive bypass."""

    import azure.mgmt.authorization as authmod  # type: ignore[import-not-found]
    from api.tasks.azure import peering_nsg

    class _BrokenAuthClient:
        def __init__(self, _cred: Any, _sub: str) -> None:
            class _P:
                def list_for_resource(self, **_kwargs: Any) -> Any:
                    raise RuntimeError("ARM unreachable")

            self.permissions = _P()

    monkeypatch.setattr(authmod, "AuthorizationManagementClient", _BrokenAuthClient)

    assert not peering_nsg.has_nsg_write_permission(
        object(),
        subscription_id="sub-target",
        resource_group="rg-target",
        nsg_name="nsg-frontend",
    )


# ---------------------------------------------------------------------------
# apply_inbound_allow_rule
# ---------------------------------------------------------------------------


def test_apply_rule_writes_when_absent(
    monkeypatch: pytest.MonkeyPatch, aks_vnet_id: str
) -> None:
    from api.tasks.azure import peering_nsg

    network = _FakeNetworkClient(vnets={}, rules=[])

    def fake_nc(_cred: Any, _sub: str) -> _FakeNetworkClient:
        return network

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    out = peering_nsg.apply_inbound_allow_rule(
        object(),
        nsg_subscription_id="sub-target",
        nsg_resource_group="rg-target",
        nsg_name="nsg-frontend",
        aks_vnet_id=aks_vnet_id,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
    )
    assert out.applied
    assert out.priority is not None
    assert peering_nsg.RULE_PRIORITY_MIN <= out.priority <= peering_nsg.RULE_PRIORITY_MAX
    assert len(network.security_rules.created) == 1
    body = network.security_rules.created[0]
    assert body["source_address_prefixes"] == ["10.224.0.0/12"]
    assert body["destination_address_prefix"] == "10.0.1.50/32"
    assert sorted(body["destination_port_ranges"]) == ["443", "80"]
    assert body["access"] == "Allow"
    assert body["direction"] == "Inbound"


def test_apply_rule_idempotent_when_existing_matches(
    monkeypatch: pytest.MonkeyPatch, aks_vnet_id: str
) -> None:
    from api.tasks.azure import peering_nsg

    expected_name = peering_nsg._deterministic_rule_name(aks_vnet_id, "10.0.1.50")
    existing = _FakeSecurityRule(
        name=expected_name,
        priority=4001,
        source_address_prefixes=["10.224.0.0/12"],
        destination_address_prefix="10.0.1.50/32",
        destination_port_ranges=["80", "443"],
    )
    network = _FakeNetworkClient(vnets={}, rules=[existing])

    def fake_nc(_cred: Any, _sub: str) -> _FakeNetworkClient:
        return network

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    out = peering_nsg.apply_inbound_allow_rule(
        object(),
        nsg_subscription_id="sub-target",
        nsg_resource_group="rg-target",
        nsg_name="nsg-frontend",
        aks_vnet_id=aks_vnet_id,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
    )
    assert out.applied
    assert out.skipped_reason == "already_present"
    assert out.priority == 4001
    assert network.security_rules.created == []


def test_apply_rule_refuses_name_collision_with_different_content(
    monkeypatch: pytest.MonkeyPatch, aks_vnet_id: str
) -> None:
    from api.tasks.azure import peering_nsg

    expected_name = peering_nsg._deterministic_rule_name(aks_vnet_id, "10.0.1.50")
    # Same name but destination differs — operator-authored rule.
    existing = _FakeSecurityRule(
        name=expected_name,
        priority=4002,
        source_address_prefix="172.16.0.0/12",
        destination_address_prefix="10.0.1.99/32",
        destination_port_range="22",
    )
    network = _FakeNetworkClient(vnets={}, rules=[existing])

    def fake_nc(_cred: Any, _sub: str) -> _FakeNetworkClient:
        return network

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    out = peering_nsg.apply_inbound_allow_rule(
        object(),
        nsg_subscription_id="sub-target",
        nsg_resource_group="rg-target",
        nsg_name="nsg-frontend",
        aks_vnet_id=aks_vnet_id,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
    )
    assert not out.applied
    assert out.skipped_reason == "name_collision"
    assert out.conflict_existing is not None
    assert network.security_rules.created == []


def test_apply_rule_picks_next_free_priority(
    monkeypatch: pytest.MonkeyPatch, aks_vnet_id: str
) -> None:
    from api.tasks.azure import peering_nsg

    # Block 4000 and 4001 with unrelated rules.
    blockers = [
        _FakeSecurityRule(name="other-1", priority=4000),
        _FakeSecurityRule(name="other-2", priority=4001),
    ]
    network = _FakeNetworkClient(vnets={}, rules=blockers)

    def fake_nc(_cred: Any, _sub: str) -> _FakeNetworkClient:
        return network

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    out = peering_nsg.apply_inbound_allow_rule(
        object(),
        nsg_subscription_id="sub-target",
        nsg_resource_group="rg-target",
        nsg_name="nsg-frontend",
        aks_vnet_id=aks_vnet_id,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80],
    )
    assert out.applied
    assert out.priority == 4002


def test_apply_rule_rejects_port_outside_allowlist(aks_vnet_id: str) -> None:
    from api.tasks.azure import peering_nsg

    with pytest.raises(ValueError):
        peering_nsg.apply_inbound_allow_rule(
            object(),
            nsg_subscription_id="sub-target",
            nsg_resource_group="rg-target",
            nsg_name="nsg-frontend",
            aks_vnet_id=aks_vnet_id,
            source_prefixes=["10.224.0.0/12"],
            destination_ip="10.0.1.50",
            ports=[22],
        )


def test_apply_rule_rejects_non_ipv4_destination(aks_vnet_id: str) -> None:
    from api.tasks.azure import peering_nsg

    with pytest.raises(ValueError):
        peering_nsg.apply_inbound_allow_rule(
            object(),
            nsg_subscription_id="sub-target",
            nsg_resource_group="rg-target",
            nsg_name="nsg-frontend",
            aks_vnet_id=aks_vnet_id,
            source_prefixes=["10.224.0.0/12"],
            destination_ip="fd00::1",
            ports=[80],
        )


def test_apply_rule_rejects_empty_source_prefixes(aks_vnet_id: str) -> None:
    from api.tasks.azure import peering_nsg

    with pytest.raises(ValueError):
        peering_nsg.apply_inbound_allow_rule(
            object(),
            nsg_subscription_id="sub-target",
            nsg_resource_group="rg-target",
            nsg_name="nsg-frontend",
            aks_vnet_id=aks_vnet_id,
            source_prefixes=[],
            destination_ip="10.0.1.50",
            ports=[80],
        )


# ---------------------------------------------------------------------------
# protocol gate (#3) — UDP / Icmp must NOT count as "already covers TCP"
# ---------------------------------------------------------------------------


def test_existing_matches_rejects_udp_rule_with_same_scope(aks_vnet_id: str) -> None:
    """A UDP allow rule with otherwise-identical scope must not be treated as
    covering the requested TCP traffic — otherwise the dashboard would
    declare ``already_present`` and the probe would still fail."""
    from api.tasks.azure import peering_nsg

    udp_existing = _FakeSecurityRule(
        name=peering_nsg._deterministic_rule_name(aks_vnet_id, "10.0.1.50"),
        priority=4001,
        source_address_prefixes=["10.224.0.0/12"],
        destination_address_prefix="10.0.1.50/32",
        destination_port_ranges=["80", "443"],
        protocol="Udp",
    )
    assert not peering_nsg._existing_matches(
        udp_existing,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
    )


def test_existing_matches_accepts_wildcard_protocol(aks_vnet_id: str) -> None:
    """An ``Asterisk`` / ``*`` protocol rule still covers TCP, so the gate
    must accept it. Belt-and-braces against an over-strict #3 fix."""
    from api.tasks.azure import peering_nsg

    wildcard = _FakeSecurityRule(
        name=peering_nsg._deterministic_rule_name(aks_vnet_id, "10.0.1.50"),
        priority=4001,
        source_address_prefixes=["10.224.0.0/12"],
        destination_address_prefix="10.0.1.50/32",
        destination_port_ranges=["80", "443"],
        protocol="*",
    )
    assert peering_nsg._existing_matches(
        wildcard,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
    )


# ---------------------------------------------------------------------------
# _summarise_rule (#6) — name_collision diagnostic payload
# ---------------------------------------------------------------------------


def test_summarise_rule_emits_diagnostic_fields() -> None:
    from api.tasks.azure import peering_nsg

    existing = _FakeSecurityRule(
        name="elb-dashboard-allow-aks-abc",
        priority=4002,
        source_address_prefix="172.16.0.0/12",
        destination_address_prefix="10.0.1.99/32",
        destination_port_range="22",
        access="Allow",
        direction="Inbound",
        protocol="Tcp",
    )
    summary = peering_nsg._summarise_rule(existing)
    assert summary["name"] == "elb-dashboard-allow-aks-abc"
    assert summary["priority"] == 4002
    assert summary["protocol"] == "Tcp"
    assert summary["access"] == "Allow"
    assert summary["direction"] == "Inbound"
    # source_address_prefixes is normalised to a list even when the SDK
    # exposes the singular field.
    assert "172.16.0.0/12" in summary["source_address_prefixes"]
    assert summary["destination_address_prefix"] == "10.0.1.99/32"
    assert "22" in summary["destination_port_ranges"]


def test_summarise_rule_exposes_plural_destination_prefixes() -> None:
    """#13 regression — an existing rule that uses ``destinationAddressPrefixes``
    (list form, e.g. operator-authored rule) must still appear in the
    summariser's plural field so the SPA's ConflictExistingPanel renders
    the full destination set instead of a blank ``(any)``."""
    from api.tasks.azure import peering_nsg

    existing = _FakeSecurityRule(
        name="operator-allow-https",
        priority=4002,
        source_address_prefix="172.16.0.0/12",
        destination_address_prefixes=["10.0.1.99/32", "10.0.1.100/32"],
        destination_port_range="443",
    )
    summary = peering_nsg._summarise_rule(existing)
    assert summary["destination_address_prefixes"] == [
        "10.0.1.99/32",
        "10.0.1.100/32",
    ]
    # Singular field stays None when the SDK exposed the plural form
    # only; the SPA falls back to the plural list for rendering.
    assert summary["destination_address_prefix"] is None


# ---------------------------------------------------------------------------
# _retry_arm (#10) — exponential backoff + Retry-After + give-up
# ---------------------------------------------------------------------------


class _FakeArmResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


def _make_http_error(status: int, headers: dict[str, str] | None = None) -> Exception:
    from azure.core.exceptions import HttpResponseError

    exc = HttpResponseError(message=f"status={status}")
    # The helper reads ``status_code`` off the exception itself (set by
    # the SDK when a response is attached); set it directly so the gate
    # in ``_is_retryable`` sees the value.
    exc.status_code = status  # type: ignore[attr-defined]
    exc.response = _FakeArmResponse(status, headers)  # type: ignore[assignment]
    return exc


def test_retry_arm_retries_on_429_then_succeeds() -> None:
    from api.tasks.azure import peering_nsg

    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_http_error(429, {"Retry-After": "0"})
        return "ok"

    sleeps: list[float] = []

    out = peering_nsg._retry_arm(
        fn,
        op_label="test.retry_429",
        attempts=4,
        sleep=sleeps.append,
    )
    assert out == "ok"
    assert calls["n"] == 3
    # Two retries → two sleep calls.
    assert len(sleeps) == 2


def test_retry_arm_honors_retry_after_seconds() -> None:
    from api.tasks.azure import peering_nsg

    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_http_error(503, {"Retry-After": "5"})
        return "ok"

    sleeps: list[float] = []
    peering_nsg._retry_arm(
        fn,
        op_label="test.retry_after",
        attempts=3,
        sleep=sleeps.append,
    )
    # Retry-After of 5s wins over the exponential schedule's 1s on the
    # first retry.
    assert sleeps and sleeps[0] >= 5.0


def test_retry_arm_gives_up_on_non_retryable_status() -> None:
    from api.tasks.azure import peering_nsg

    def fn() -> str:
        raise _make_http_error(403)

    with pytest.raises(Exception) as excinfo:
        peering_nsg._retry_arm(
            fn,
            op_label="test.non_retryable",
            attempts=4,
            sleep=lambda _s: None,
        )
    # Non-retryable: caller sees the original 403 without further sleeps.
    assert "status=403" in str(excinfo.value)


# ---------------------------------------------------------------------------
# dry_run (#7) — preview path must not call begin_create_or_update
# ---------------------------------------------------------------------------


def test_apply_rule_dry_run_does_not_write_when_would_create(
    monkeypatch: pytest.MonkeyPatch, aks_vnet_id: str
) -> None:
    from api.tasks.azure import peering_nsg

    network = _FakeNetworkClient(vnets={}, rules=[])

    def fake_nc(_cred: Any, _sub: str) -> _FakeNetworkClient:
        return network

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    out = peering_nsg.apply_inbound_allow_rule(
        object(),
        nsg_subscription_id="sub-target",
        nsg_resource_group="rg-target",
        nsg_name="nsg-frontend",
        aks_vnet_id=aks_vnet_id,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
        dry_run=True,
    )
    assert not out.applied
    assert out.skipped_reason == "dry_run"
    assert out.priority is not None
    assert network.security_rules.created == []


def test_apply_rule_dry_run_still_reports_already_present(
    monkeypatch: pytest.MonkeyPatch, aks_vnet_id: str
) -> None:
    """dry_run preview must produce the same idempotent terminal states
    as the apply path so the SPA can pre-warn the operator that no write
    is required."""
    from api.tasks.azure import peering_nsg

    expected_name = peering_nsg._deterministic_rule_name(aks_vnet_id, "10.0.1.50")
    existing = _FakeSecurityRule(
        name=expected_name,
        priority=4001,
        source_address_prefixes=["10.224.0.0/12"],
        destination_address_prefix="10.0.1.50/32",
        destination_port_ranges=["80", "443"],
    )
    network = _FakeNetworkClient(vnets={}, rules=[existing])

    def fake_nc(_cred: Any, _sub: str) -> _FakeNetworkClient:
        return network

    monkeypatch.setattr(peering_nsg, "network_client", fake_nc)

    out = peering_nsg.apply_inbound_allow_rule(
        object(),
        nsg_subscription_id="sub-target",
        nsg_resource_group="rg-target",
        nsg_name="nsg-frontend",
        aks_vnet_id=aks_vnet_id,
        source_prefixes=["10.224.0.0/12"],
        destination_ip="10.0.1.50",
        ports=[80, 443],
        dry_run=True,
    )
    # already_present wins; dry_run does not change the terminal state.
    assert out.applied
    assert out.skipped_reason == "already_present"
    assert network.security_rules.created == []


def test_deterministic_rule_name_is_exposed_for_route_layer(aks_vnet_id: str) -> None:
    from api.tasks.azure import peering_nsg

    public = peering_nsg.deterministic_rule_name(aks_vnet_id, "10.0.1.50")
    private = peering_nsg._deterministic_rule_name(aks_vnet_id, "10.0.1.50")
    assert public == private
