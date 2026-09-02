"""Tests for the runtime ACR build-access lease service.

Responsibility: Verify bounded open/restore policy, stale-open healing,
concurrent build deferral, and failure-safe network behavior with fake SDKs.
Edit boundaries: Test-only; no Azure calls or real sleeps.
Key entry points: pytest test functions.
Risky contracts: Restore failures must return False; active or unknown builds
must leave ACR open; preserve-open is explicit only.
Validation: `uv run pytest -q api/tests/test_acr_build_access_service.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services import acr_build_access


class _Poller:
    def result(self, timeout: int) -> None:
        assert timeout == 120


class _Registries:
    def __init__(
        self,
        state: tuple[str, str, str],
        *,
        private_endpoint_ready: bool = True,
        ip_rules: tuple[str, ...] = (),
    ) -> None:
        self.state = state
        self.private_endpoint_ready = private_endpoint_ready
        self.ip_rules = ip_rules
        self.updates: list[tuple[str, str, str]] = []

    def get(self, _resource_group: str, _registry: str) -> object:
        public, default_action, bypass = self.state
        return SimpleNamespace(
            public_network_access=public,
            network_rule_set=SimpleNamespace(
                default_action=default_action,
                ip_rules=[SimpleNamespace(ip_address_or_range=value) for value in self.ip_rules],
            ),
            network_rule_bypass_options=bypass,
            private_endpoint_connections=(
                [
                    SimpleNamespace(
                        private_link_service_connection_state=SimpleNamespace(status="Approved")
                    )
                ]
                if self.private_endpoint_ready
                else []
            ),
        )

    def begin_update(
        self,
        _resource_group: str,
        _registry: str,
        parameters: object,
    ) -> _Poller:
        state = (
            str(parameters.public_network_access),
            str(parameters.network_rule_set.default_action),
            str(parameters.network_rule_bypass_options),
        )
        self.state = state
        self.ip_rules = tuple(
            str(rule.ip_address_or_range) for rule in parameters.network_rule_set.ip_rules
        )
        self.updates.append(state)
        return _Poller()


class _Client:
    def __init__(
        self,
        state: tuple[str, str, str],
        *,
        private_endpoint_ready: bool = True,
        ip_rules: tuple[str, ...] = (),
    ) -> None:
        self.registries = _Registries(
            state,
            private_endpoint_ready=private_endpoint_ready,
            ip_rules=ip_rules,
        )


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    state: tuple[str, str, str],
    *,
    private_endpoint_ready: bool = True,
    ip_rules: tuple[str, ...] = (),
) -> _Client:
    client = _Client(
        state,
        private_endpoint_ready=private_endpoint_ready,
        ip_rules=ip_rules,
    )
    monkeypatch.setattr(acr_build_access, "acr_client", lambda *_args: client)
    monkeypatch.setattr(acr_build_access.time, "sleep", lambda _seconds: None)
    return client


def test_private_registry_opens_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch_client(monkeypatch, ("Disabled", "Deny", "AzureServices"))
    monkeypatch.setattr(acr_build_access, "_active_build_count", lambda *_a, **_k: 0)

    lease = acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=0,
    )
    restored = acr_build_access.restore_build_access(object(), lease)

    assert restored is True
    assert client.registries.updates == [
        ("Enabled", "Allow", "AzureServices"),
        ("Disabled", "Deny", "AzureServices"),
    ]


def test_open_waits_for_build_agent_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(("Disabled", "Deny", "AzureServices"))
    sleeps: list[float] = []
    monkeypatch.setattr(acr_build_access, "acr_client", lambda *_args: client)
    monkeypatch.setattr(acr_build_access.time, "sleep", sleeps.append)

    acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=5,
        settle_seconds=75,
    )

    assert sleeps == [75]


def test_idle_stale_open_registry_heals_private(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch_client(monkeypatch, ("Enabled", "Allow", "AzureServices"))
    monkeypatch.setattr(acr_build_access, "_active_build_count", lambda *_a, **_k: 0)

    lease = acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=0,
    )

    assert lease.restore_to_private is True
    assert acr_build_access.restore_build_access(object(), lease) is True
    assert client.registries.updates == [("Disabled", "Deny", "AzureServices")]


def test_explicit_preserve_keeps_open_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch_client(monkeypatch, ("Enabled", "Allow", "AzureServices"))
    monkeypatch.setenv("ACR_BUILD_ACCESS_PRESERVE_OPEN", "true")

    lease = acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=0,
    )

    assert lease.restore_required is False
    assert acr_build_access.restore_build_access(object(), lease) is True
    assert client.registries.updates == []


def test_stale_open_without_private_endpoint_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _patch_client(
        monkeypatch,
        ("Enabled", "Allow", "AzureServices"),
        private_endpoint_ready=False,
    )

    with pytest.raises(RuntimeError, match="no approved private endpoint"):
        acr_build_access.open_build_access(
            object(),
            subscription_id="sub",
            resource_group="rg",
            registry_name="acr",
            interval_seconds=0,
        )

    assert client.registries.updates == []


def test_original_ip_allowlist_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch_client(
        monkeypatch,
        ("Enabled", "Deny", "AzureServices"),
        ip_rules=("203.0.113.4/32",),
    )
    monkeypatch.setattr(acr_build_access, "_active_build_count", lambda *_a, **_k: 0)

    lease = acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=0,
        settle_seconds=0,
    )
    assert acr_build_access.restore_build_access(object(), lease) is True

    assert client.registries.state == ("Enabled", "Deny", "AzureServices")
    assert client.registries.ip_rules == ("203.0.113.4/32",)


def test_active_other_build_defers_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch_client(monkeypatch, ("Disabled", "Deny", "AzureServices"))
    monkeypatch.setattr(acr_build_access, "_active_build_count", lambda *_a, **_k: 1)
    lease = acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=0,
    )

    assert acr_build_access.restore_build_access(object(), lease) is False
    assert client.registries.state == ("Enabled", "Allow", "AzureServices")


def test_unknown_active_build_state_defers_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch_client(monkeypatch, ("Disabled", "Deny", "AzureServices"))
    monkeypatch.setattr(
        acr_build_access,
        "_active_build_count",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("query failed")),
    )
    lease = acr_build_access.open_build_access(
        object(),
        subscription_id="sub",
        resource_group="rg",
        registry_name="acr",
        interval_seconds=0,
    )

    assert acr_build_access.restore_build_access(object(), lease) is False
    assert client.registries.state == ("Enabled", "Allow", "AzureServices")


def test_open_propagation_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NeverConverges(_Registries):
        def get(self, _resource_group: str, _registry: str) -> object:
            return SimpleNamespace(
                public_network_access="Disabled",
                network_rule_set=SimpleNamespace(default_action="Deny"),
                network_rule_bypass_options="AzureServices",
            )

    client = _Client(("Disabled", "Deny", "AzureServices"))
    client.registries = _NeverConverges(("Disabled", "Deny", "AzureServices"))
    monkeypatch.setattr(acr_build_access, "acr_client", lambda *_args: client)
    monkeypatch.setattr(acr_build_access.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="did not become effective"):
        acr_build_access.open_build_access(
            object(),
            subscription_id="sub",
            resource_group="rg",
            registry_name="acr",
            max_attempts=2,
            interval_seconds=0,
        )

    assert client.registries.updates == [
        ("Enabled", "Allow", "AzureServices"),
        ("Disabled", "Deny", "AzureServices"),
    ]
