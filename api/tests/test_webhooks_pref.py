"""Tests for webhook config + SSRF-safe URL validation.

Responsibility: Lock the SSRF allowlist (https-only, IP-literal reject, host
allowlist incl. WEBHOOK_ALLOWED_HOSTS), URL masking, and config save/get.
Edit boundaries: Test-only; monkeypatches the table client.
Key entry points: pytest test functions.
Risky contracts: validate_webhook_url is the SSRF gate — these tests guard it.
Validation: ``uv run pytest -q api/tests/test_webhooks_pref.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services import webhooks_pref as wp
from azure.core.exceptions import ResourceNotFoundError


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.slack.com/services/T1/B2/abc",
        "https://mytenant.webhook.office.com/webhookb2/abc",
        "https://discord.com/api/webhooks/1/x",
        "https://x.logic.azure.com/workflows/y/triggers/z",
    ],
)
def test_valid_urls_accepted(url: str) -> None:
    assert wp.validate_webhook_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/x",  # not https
        "https://10.0.0.1/x",  # IP literal
        "https://169.254.169.254/x",  # metadata IP
        "https://evil.com/x",  # not allowlisted
        "https://hooks.slack.com.evil.com/x",  # suffix trick
        "ftp://hooks.slack.com/x",  # bad scheme
    ],
)
def test_invalid_urls_rejected(url: str) -> None:
    with pytest.raises(wp.WebhookValidationError):
        wp.validate_webhook_url(url)


def test_empty_url_allowed() -> None:
    assert wp.validate_webhook_url("") == ""


def test_extra_allowed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_ALLOWED_HOSTS", "my.internal.hook")
    assert wp.validate_webhook_url("https://my.internal.hook/x")


def test_mask_hides_secret_tail() -> None:
    masked = wp.mask_url("https://hooks.slack.com/services/T123/B456/shhsecret")
    assert "shhsecret" not in masked
    assert "hooks.slack.com" in masked


class FakeTable:
    def __init__(self, store: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.store = store

    def __enter__(self) -> FakeTable:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        key = (partition_key, row_key)
        if key not in self.store:
            raise ResourceNotFoundError("missing")
        return dict(self.store[key])

    def upsert_entity(self, entity: dict[str, Any], mode: Any = None) -> None:
        del mode
        self.store[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], dict[str, Any]]:
    data: dict[tuple[str, str], dict[str, Any]] = {}
    monkeypatch.setattr(wp, "_ensure_table", lambda: None)
    monkeypatch.setattr(wp, "_table_client", lambda: FakeTable(data))
    return data


def test_save_and_get(store: dict) -> None:
    wp.save_config(
        url="https://hooks.slack.com/services/a/b/c", enabled=True, events="terminal"
    )
    cfg = wp.get_config()
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.url.startswith("https://hooks.slack.com")
    assert cfg.public_dict()["url_masked"].endswith("/***")


def test_save_invalid_raises(store: dict) -> None:
    with pytest.raises(wp.WebhookValidationError):
        wp.save_config(url="https://evil.com/x", enabled=True, events="terminal")


def test_empty_url_forces_disabled(store: dict) -> None:
    cfg = wp.save_config(url="", enabled=True, events="terminal")
    assert cfg.enabled is False
    assert cfg.url == ""


def test_blank_url_keeps_existing(store: dict) -> None:
    wp.save_config(
        url="https://hooks.slack.com/services/a/b/c", enabled=True, events="terminal"
    )
    cfg = wp.save_config(url="", enabled=False, events="failed_only")
    assert cfg.url.startswith("https://hooks.slack.com")  # kept
    assert cfg.enabled is False
    assert cfg.events == "failed_only"


def test_get_unset_returns_none(store: dict) -> None:
    assert wp.get_config() is None
