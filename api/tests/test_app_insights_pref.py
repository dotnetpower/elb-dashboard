"""Tests for the durable App Insights connection-string store + fallback.

Responsibility: Verify the file-backed persistence roundtrip, the malformed /
missing / corrupt guards, and that `deployment_connection_string()` and the
telemetry resolver fall back to the persisted row when the env var is empty.
Edit boundaries: Exercise the local file backend only (no Azure Table); drive
the public API of `api.services.app_insights_pref`.
Key entry points: `test_roundtrip_and_clear`, `test_get_empty_when_missing`,
    `test_save_rejects_malformed`, `test_deployment_connection_string_fallback`,
    `test_telemetry_resolver_prefers_env`.
Risky contracts: `get_persisted_connection_string` must never raise; the
    fallback must not fire while the env var is set.
Validation: `uv run pytest -q api/tests/test_app_insights_pref.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Force the local file backend into a tmp dir so tests never touch a real
    # Table or a developer's `.logs/local/state` file.
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))


_VALID = "InstrumentationKey=abc123;IngestionEndpoint=https://example.local/"


def test_roundtrip_and_clear() -> None:
    from api.services.app_insights_pref import (
        clear_persisted_connection_string,
        get_persisted_connection_string,
        save_persisted_connection_string,
    )

    assert get_persisted_connection_string() == ""
    save_persisted_connection_string(_VALID, owner_oid="oid-1", tenant_id="tid-1")
    assert get_persisted_connection_string() == _VALID
    clear_persisted_connection_string()
    assert get_persisted_connection_string() == ""


def test_clear_is_idempotent_when_missing() -> None:
    from api.services.app_insights_pref import clear_persisted_connection_string

    clear_persisted_connection_string()  # no row yet — must not raise
    clear_persisted_connection_string()


def test_save_rejects_malformed() -> None:
    from api.services.app_insights_pref import (
        get_persisted_connection_string,
        save_persisted_connection_string,
    )

    for bad in ("", "   ", "no-key-here", "x" * 5000):
        with pytest.raises(ValueError):
            save_persisted_connection_string(bad)
    assert get_persisted_connection_string() == ""


def test_get_never_raises_on_corrupt_file(tmp_path: Path) -> None:
    from api.services import app_insights_pref

    # Write a corrupt JSON payload where the store expects its file.
    corrupt = app_insights_pref._state_file()
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{ not json", encoding="utf-8")
    assert app_insights_pref.get_persisted_connection_string() == ""


def test_deployment_connection_string_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.app_insights_pref import save_persisted_connection_string
    from api.services.app_insights_provisioning import deployment_connection_string

    save_persisted_connection_string(_VALID)

    # Env wins when present (no fallback read).
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=env;")
    assert deployment_connection_string() == "InstrumentationKey=env;"

    # Env empty → heal from the persisted row.
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    assert deployment_connection_string() == _VALID


def test_telemetry_resolver_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.app import telemetry
    from api.services.app_insights_pref import save_persisted_connection_string

    save_persisted_connection_string(_VALID)

    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=env;")
    assert telemetry._resolve_connection_string() == "InstrumentationKey=env;"

    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    assert telemetry._resolve_connection_string() == _VALID
