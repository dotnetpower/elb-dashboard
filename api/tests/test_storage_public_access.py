"""Tests for api.services.storage_public_access - local-debug helper.

Responsibility: Tests for api.services.storage_public_access - local-debug helper
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_clear_env`, `test_gate_disabled_by_default`,
`test_gate_enabled_only_when_opt_in_set`, `test_gate_falsy_values`,
`test_gate_blocked_in_container_app`, `test_ensure_noop_when_gate_disabled`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_storage_public_access.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from api.services.storage import public_access as spa


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(spa.ENV_OPT_IN, raising=False)
    monkeypatch.delenv(spa.ENV_CONTAINER_APP, raising=False)
    monkeypatch.delenv("ELB_LOCAL_CALLER_IP", raising=False)
    # Clear the in-process TTL cache so tests are independent.
    with spa._cache_lock:
        spa._already_open_cache.clear()
    with spa._caller_ip_lock:
        spa._caller_ip_cache = None


def test_gate_disabled_by_default() -> None:
    assert spa.is_local_debug_auto_open_enabled() is False


def test_gate_enabled_only_when_opt_in_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    assert spa.is_local_debug_auto_open_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off"])
def test_gate_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(spa.ENV_OPT_IN, value)
    assert spa.is_local_debug_auto_open_enabled() is False


def test_gate_blocked_in_container_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operational guard: even with opt-in, a Container App MUST refuse."""
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    monkeypatch.setenv(spa.ENV_CONTAINER_APP, "ca-elb-dashboard")
    assert spa.is_local_debug_auto_open_enabled() is False


def test_ensure_noop_when_gate_disabled() -> None:
    cred = MagicMock()
    result = spa.ensure_local_storage_access(cred, "sub", "rg", "elbstg01")
    assert result["action"] == "noop"


def test_ensure_noop_in_container_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    monkeypatch.setenv(spa.ENV_CONTAINER_APP, "ca-elb-dashboard")
    cred = MagicMock()
    result = spa.ensure_local_storage_access(cred, "sub", "rg", "elbstg01")
    assert result["action"] == "noop"


def _make_account(
    public: str,
    default_action: str = "Deny",
    ip_rules: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        public_network_access=public,
        network_rule_set=SimpleNamespace(
            default_action=default_action,
            ip_rules=[SimpleNamespace(ip_address_or_range=ip) for ip in (ip_rules or [])],
            virtual_network_rules=[],
        ),
    )


def test_ensure_already_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # Already open = publicNetworkAccess=Enabled + defaultAction=Allow.
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    sc = MagicMock()
    acct = _make_account("Enabled", default_action="Allow")
    sc.storage_accounts.get_properties.return_value = acct
    with patch("api.services.azure_clients.storage_client", return_value=sc):
        result = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
    assert result["action"] == "already_open"
    assert result["default_action"] == "Allow"
    sc.storage_accounts.update.assert_not_called()


def test_ensure_opens_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    sc = MagicMock()
    acct = _make_account("Disabled", default_action="Deny")
    sc.storage_accounts.get_properties.return_value = acct
    with (
        patch("api.services.azure_clients.storage_client", return_value=sc),
        patch.object(spa, "_detect_caller_ip", return_value="9.9.9.9"),
    ):
        result = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
    assert result["action"] == "opened"
    assert result["ip"] == "9.9.9.9"
    assert result["previous_public"] == "Disabled"
    assert result["default_action"] == "Allow"
    assert "storage-public-access.sh off" in result["off_hint"]
    sc.storage_accounts.update.assert_called_once()
    args, _ = sc.storage_accounts.update.call_args
    assert args[0] == "rg"
    assert args[1] == "elbstg01"
    update_params = args[2]
    assert update_params.public_network_access == "Enabled"
    # New strategy: defaultAction=Allow, no per-IP rules.
    assert update_params.network_rule_set.default_action == "Allow"
    assert not getattr(update_params.network_rule_set, "ip_rules", None)


def test_ensure_updates_to_allow_when_enabled_with_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Enabled+Deny (e.g. old Deny+ipRule state) is not "already open".
    # The function must update to Allow regardless of existing ip_rules.
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    sc = MagicMock()
    sc.storage_accounts.get_properties.return_value = _make_account(
        "Enabled", default_action="Deny", ip_rules=["1.1.1.1"]
    )
    with (
        patch("api.services.azure_clients.storage_client", return_value=sc),
        patch.object(spa, "_detect_caller_ip", return_value="2.2.2.2"),
    ):
        result = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
    assert result["action"] == "opened"
    assert result["default_action"] == "Allow"
    args, _ = sc.storage_accounts.update.call_args
    update_params = args[2]
    assert update_params.network_rule_set.default_action == "Allow"
    assert not getattr(update_params.network_rule_set, "ip_rules", None)


def test_ensure_returns_failed_on_arm_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    sc = MagicMock()
    sc.storage_accounts.get_properties.side_effect = RuntimeError("boom")
    with patch("api.services.azure_clients.storage_client", return_value=sc):
        result = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
    assert result["action"] == "failed"
    assert "arm_read" in result["error"]


def test_ensure_opened_when_caller_ip_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # IP detection is now informational only; a None result does NOT block the update.
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    sc = MagicMock()
    acct = _make_account("Disabled", default_action="Deny")
    sc.storage_accounts.get_properties.return_value = acct
    with (
        patch("api.services.azure_clients.storage_client", return_value=sc),
        patch.object(spa, "_detect_caller_ip", return_value=None),
    ):
        result = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
    assert result["action"] == "opened"
    assert result["default_action"] == "Allow"
    assert "ip" not in result  # no IP to report
    sc.storage_accounts.update.assert_called_once()


def test_ensure_already_open_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call within TTL must NOT hit ARM (CPU hot path)."""
    monkeypatch.setenv(spa.ENV_OPT_IN, "true")
    sc = MagicMock()
    # Already-open state: Enabled + defaultAction=Allow.
    acct = _make_account("Enabled", default_action="Allow")
    sc.storage_accounts.get_properties.return_value = acct
    with patch("api.services.azure_clients.storage_client", return_value=sc):
        first = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
        second = spa.ensure_local_storage_access(MagicMock(), "sub", "rg", "elbstg01")
    assert first["action"] == "already_open"
    assert second["action"] == "already_open"
    # Cache hit: ARM read only fired once, no IP detection needed.
    assert sc.storage_accounts.get_properties.call_count == 1


# ---------------------------------------------------------------------------
# Security audit (2026-05-22): #10 caller-IP cache + fallback + env override
# ---------------------------------------------------------------------------
def test_detect_caller_ip_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ELB_LOCAL_CALLER_IP` short-circuits the external probe."""
    monkeypatch.setenv("ELB_LOCAL_CALLER_IP", "203.0.113.7")

    class _ShouldNotCall:
        def __call__(self, *_a: object, **_kw: object) -> object:
            raise AssertionError("env override must skip the network")

    monkeypatch.setattr("httpx.get", _ShouldNotCall())

    assert spa._detect_caller_ip() == "203.0.113.7"


def test_detect_caller_ip_env_override_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-IPv4 override is ignored — must not poison the log/UI strings."""
    monkeypatch.setenv("ELB_LOCAL_CALLER_IP", "not-an-ip")

    calls: list[str] = []

    def fake_get(url: str, **_kw: object) -> SimpleNamespace:
        calls.append(url)
        return SimpleNamespace(status_code=200, text="198.51.100.4")

    monkeypatch.setattr("httpx.get", fake_get)

    assert spa._detect_caller_ip() == "198.51.100.4"
    assert calls, "must have fallen through to the network providers"


def test_detect_caller_ip_caches_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-to-back polls share a single HTTP call."""
    n_calls = {"count": 0}

    def fake_get(_url: str, **_kw: object) -> SimpleNamespace:
        n_calls["count"] += 1
        return SimpleNamespace(status_code=200, text="198.51.100.4")

    monkeypatch.setattr("httpx.get", fake_get)

    assert spa._detect_caller_ip() == "198.51.100.4"
    assert spa._detect_caller_ip() == "198.51.100.4"
    assert spa._detect_caller_ip() == "198.51.100.4"
    assert n_calls["count"] == 1


def test_detect_caller_ip_falls_back_to_second_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 / network blip on the first provider rolls over to the second."""
    seen: list[str] = []

    def fake_get(url: str, **_kw: object) -> SimpleNamespace:
        seen.append(url)
        if url == spa._CALLER_IP_LOOKUP_URLS[0]:
            raise RuntimeError("dns blip")
        return SimpleNamespace(status_code=200, text="192.0.2.5")

    monkeypatch.setattr("httpx.get", fake_get)

    assert spa._detect_caller_ip() == "192.0.2.5"
    assert seen == list(spa._CALLER_IP_LOOKUP_URLS)


def test_detect_caller_ip_caches_failure_with_short_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All providers down: cache None so the poll cadence does not spam."""

    def fake_get(_url: str, **_kw: object) -> SimpleNamespace:
        return SimpleNamespace(status_code=503, text="")

    monkeypatch.setattr("httpx.get", fake_get)

    assert spa._detect_caller_ip() is None
    # Second call must come from the cache (not hit the network again).
    n_calls = {"count": 0}

    def fake_get_2(_url: str, **_kw: object) -> SimpleNamespace:
        n_calls["count"] += 1
        return SimpleNamespace(status_code=200, text="should-not-be-used")

    monkeypatch.setattr("httpx.get", fake_get_2)
    assert spa._detect_caller_ip() is None
    assert n_calls["count"] == 0
