"""Tests for the NCBI API key preference store + Settings route.

Responsibility: Cover ``ncbi_pref`` round-trip on the local JSON backend
(save/get/clear, validation, masking, env override, TTL cache invalidation) and
the ``/api/settings/ncbi`` GET/PUT routes (dev-bypass auth, masked responses,
plaintext never returned).
Edit boundaries: Only ``api/services/ncbi_pref.py`` + ``api/routes/settings/ncbi.py``.
Key entry points: ``get_ncbi_api_key``, ``save_ncbi_api_key``,
``ncbi_settings_public``, ``/api/settings/ncbi``.
Risky contracts: Uses ``ELB_LOCAL_STATE_DIR`` (tmp) so no Table/credential is
touched; clears the in-process cache between cases.
Validation: ``uv run pytest -q api/tests/test_ncbi_pref.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _local_store(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """Force the JSON-file backend in a temp dir and a clean cache + no env key."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    from api.services.ncbi_pref import clear_ncbi_pref_cache

    clear_ncbi_pref_cache()


def test_save_get_roundtrip() -> None:
    from api.services.ncbi_pref import get_ncbi_api_key, save_ncbi_api_key

    assert get_ncbi_api_key() is None
    masked = save_ncbi_api_key("abcdef0123456789abcdef", owner_oid="oid-1")
    assert masked["has_key"] is True
    assert masked["source"] == "settings"
    assert masked["last4"] == "cdef"
    assert get_ncbi_api_key() == "abcdef0123456789abcdef"


def test_clear_key() -> None:
    from api.services.ncbi_pref import get_ncbi_api_key, save_ncbi_api_key

    save_ncbi_api_key("abcdef0123456789abcdef")
    masked = save_ncbi_api_key("")
    assert masked["has_key"] is False
    assert masked["source"] == "none"
    assert get_ncbi_api_key() is None


@pytest.mark.parametrize("bad", ["short", "has spaces here", "x" * 200, "bad!chars"])
def test_save_rejects_invalid_key(bad: str) -> None:
    from api.services.ncbi_pref import save_ncbi_api_key

    with pytest.raises(ValueError):
        save_ncbi_api_key(bad)


def test_env_key_wins_and_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.ncbi_pref import ncbi_settings_public, save_ncbi_api_key

    save_ncbi_api_key("settingsKey0123456789")
    monkeypatch.setenv("NCBI_API_KEY", "envKeyABCDEFGH123456")
    masked = ncbi_settings_public()
    assert masked["source"] == "env"
    assert masked["env_locked"] is True
    assert masked["last4"] == "3456"


def test_identity_params_use_stored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services.ncbi._eutils import ncbi_identity_params
    from api.services.ncbi_pref import save_ncbi_api_key

    save_ncbi_api_key("storedKey0123456789ab")
    params = ncbi_identity_params()
    assert params["api_key"] == "storedKey0123456789ab"


def test_stored_key_lifts_rate_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key saved in Settings (not just env) must raise the 3→10 req/s tier."""
    from api.services.ncbi._eutils import _rate_capacity
    from api.services.ncbi_pref import save_ncbi_api_key

    monkeypatch.delenv("NCBI_EUTILS_RATE_PER_SEC", raising=False)
    # No key anywhere → conservative 3 req/s.
    assert _rate_capacity() == (3.0, 3.0)
    # Key pasted in Settings (env still unset) → 10 req/s.
    save_ncbi_api_key("storedKey0123456789ab")
    assert _rate_capacity() == (10.0, 10.0)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def test_route_get_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).get("/api/settings/ncbi")
    assert response.status_code == 200
    assert response.json()["config"]["has_key"] is False


def test_route_put_key_masks_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).put(
        "/api/settings/ncbi", json={"api_key": "routeKey0123456789abcd"}
    )
    assert response.status_code == 200
    body = response.json()["config"]
    assert body["has_key"] is True
    assert body["last4"] == "abcd"
    # The plaintext key must never appear in the response.
    assert "routeKey0123456789abcd" not in response.text


def test_route_put_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    response = TestClient(app).put("/api/settings/ncbi", json={"api_key": "bad key!"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_api_key"
