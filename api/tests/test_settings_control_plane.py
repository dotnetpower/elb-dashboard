"""Tests for the Settings → Control plane domain service + HTTP routes.

Responsibility: Verify URL normalisation (https-only except localhost, no
    path/query), durable save/get/clear via a faked singleton store, the
    env -> settings -> Container App resolution precedence, and the GET/PUT/DELETE
    route contracts (validation, 503 on durable-store failure, never 404).
Edit boundaries: Behaviour tests only; persistence primitive (`singletons`) is
    covered by its own test module.
Key entry points: the ``test_*`` functions.
Risky contracts: every route enforces ``require_caller``; the env hard pin must
    win over the Settings value.
Validation: ``uv run pytest -q api/tests/test_settings_control_plane.py``.
"""

from __future__ import annotations

import pytest
from api.services import control_plane_url as cpu
from fastapi.testclient import TestClient


@pytest.fixture()
def fake_singleton(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """Replace the durable singleton store with an in-memory dict."""
    store: dict[str, dict] = {}

    def _save(key: str, payload: dict) -> bool:
        store[key] = dict(payload)
        return True

    def _load(key: str):
        return store.get(key)

    def _clear(key: str) -> bool:
        store.pop(key, None)
        return True

    monkeypatch.setattr("api.services.state.singletons.save_singleton", _save)
    monkeypatch.setattr("api.services.state.singletons.load_singleton", _load)
    monkeypatch.setattr("api.services.state.singletons.clear_singleton", _clear)
    return store


# --- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://dashboard.elasticblast.com", "https://dashboard.elasticblast.com"),
        ("https://dashboard.elasticblast.com/", "https://dashboard.elasticblast.com"),
        ("  https://dashboard.elasticblast.com  ", "https://dashboard.elasticblast.com"),
        ("https://dashboard.elasticblast.com:8443", "https://dashboard.elasticblast.com:8443"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
        # Mixed-case scheme + host are canonicalised to lower-case so a
        # case-sensitive `startswith("https://")` check on the sibling cannot
        # reject the stored webhook target.
        ("HTTPS://Dashboard.Example.com", "https://dashboard.example.com"),
        ("https://Dashboard.ElasticBlast.com:8443", "https://dashboard.elasticblast.com:8443"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalise_accepts(raw: str, expected: str) -> None:
    assert cpu.normalise_control_plane_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://dashboard.elasticblast.com",  # http on a public host
        "ftp://dashboard.elasticblast.com",  # wrong scheme
        "https://dashboard.elasticblast.com/api",  # path
        "https://dashboard.elasticblast.com/?x=1",  # query
        "https://dashboard.elasticblast.com/#frag",  # fragment
        "https://",  # no host
        "dashboard.elasticblast.com",  # no scheme
        "https://user:pass@dashboard.elasticblast.com",  # embedded credentials
        "https://host\tinjected.com",  # control char (tab) injection
        "https://host\ninjected.com",  # control char (newline) injection
        "https://dashboard.elasticblast.com:99999",  # invalid port
    ],
)
def test_normalise_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        cpu.normalise_control_plane_url(raw)


# --- persistence -----------------------------------------------------------


def test_save_get_clear_roundtrip(fake_singleton: dict[str, dict]) -> None:
    assert cpu.get_control_plane_url() == ""
    assert cpu.save_control_plane_url("https://dashboard.elasticblast.com/") is True
    assert cpu.get_control_plane_url() == "https://dashboard.elasticblast.com"
    assert cpu.clear_control_plane_url() is True
    assert cpu.get_control_plane_url() == ""


def test_save_rejects_invalid(fake_singleton: dict[str, dict]) -> None:
    with pytest.raises(ValueError):
        cpu.save_control_plane_url("http://dashboard.elasticblast.com")
    assert cpu.get_control_plane_url() == ""


def test_get_degrades_to_empty_on_store_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_key: str):
        raise RuntimeError("table unreachable")

    monkeypatch.setattr("api.services.state.singletons.load_singleton", _boom)
    assert cpu.get_control_plane_url() == ""


# --- resolution precedence -------------------------------------------------


def test_resolve_env_wins_over_settings(
    fake_singleton: dict[str, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    cpu.save_control_plane_url("https://dashboard.elasticblast.com")
    monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://pinned.example.com/")
    url, source = cpu.resolve_control_plane_url()
    assert url == "https://pinned.example.com"
    assert source == cpu.SOURCE_ENV


def test_resolve_settings_wins_over_container_app(
    fake_singleton: dict[str, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv("CONTAINER_APP_ENV_DNS_SUFFIX", "env.koreacentral.azurecontainerapps.io")
    cpu.save_control_plane_url("https://dashboard.elasticblast.com")
    url, source = cpu.resolve_control_plane_url()
    assert url == "https://dashboard.elasticblast.com"
    assert source == cpu.SOURCE_SETTINGS


def test_resolve_falls_back_to_container_app(
    fake_singleton: dict[str, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.setenv("CONTAINER_APP_ENV_DNS_SUFFIX", "env.koreacentral.azurecontainerapps.io")
    url, source = cpu.resolve_control_plane_url()
    assert url == "https://ca-elb-dashboard.env.koreacentral.azurecontainerapps.io"
    assert source == cpu.SOURCE_CONTAINER_APP


def test_resolve_none_when_nothing_configured(
    fake_singleton: dict[str, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("CONTAINER_APP_ENV_DNS_SUFFIX", raising=False)
    url, source = cpu.resolve_control_plane_url()
    assert url == ""
    assert source == cpu.SOURCE_NONE


# --- routes ----------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("CONTAINER_APP_ENV_DNS_SUFFIX", raising=False)
    from api.main import app

    return TestClient(app)


def test_route_get_defaults_empty(client: TestClient, fake_singleton: dict[str, dict]) -> None:
    r = client.get("/api/settings/control-plane")
    assert r.status_code == 200
    body = r.json()
    assert body["configured_url"] == ""
    assert body["effective_url"] == ""
    assert body["source"] == "none"
    assert body["container_app_url"] == ""


def test_route_put_persists_and_resolves(
    client: TestClient, fake_singleton: dict[str, dict]
) -> None:
    r = client.put(
        "/api/settings/control-plane",
        json={"url": "https://dashboard.elasticblast.com/"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "saved"
    assert body["configured_url"] == "https://dashboard.elasticblast.com"
    assert body["effective_url"] == "https://dashboard.elasticblast.com"
    assert body["source"] == "settings"

    # The GET now reflects the saved value.
    g = client.get("/api/settings/control-plane").json()
    assert g["configured_url"] == "https://dashboard.elasticblast.com"


def test_route_put_rejects_invalid(client: TestClient, fake_singleton: dict[str, dict]) -> None:
    r = client.put(
        "/api/settings/control-plane",
        json={"url": "http://dashboard.elasticblast.com"},
    )
    assert r.status_code == 400
    # The global StarletteHTTPException handler flattens a dict detail to the
    # top-level body, so the error code is read at the root (not under `detail`).
    assert r.json()["code"] == "invalid_url"


def test_route_put_requires_url(client: TestClient, fake_singleton: dict[str, dict]) -> None:
    r = client.put("/api/settings/control-plane", json={"url": "   "})
    assert r.status_code == 400
    assert r.json()["code"] == "url_required"


def test_route_put_503_when_durable_store_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.services.state.singletons.save_singleton", lambda _k, _p: False
    )
    r = client.put(
        "/api/settings/control-plane",
        json={"url": "https://dashboard.elasticblast.com"},
    )
    assert r.status_code == 503
    assert r.json()["code"] == "persist_failed"


def test_route_delete_clears(client: TestClient, fake_singleton: dict[str, dict]) -> None:
    client.put(
        "/api/settings/control-plane",
        json={"url": "https://dashboard.elasticblast.com"},
    )
    r = client.delete("/api/settings/control-plane")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cleared"
    assert body["configured_url"] == ""
