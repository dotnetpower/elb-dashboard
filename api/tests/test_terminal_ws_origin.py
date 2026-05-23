"""Tests for the terminal WebSocket Origin allowlist (CSWSH defence).

Responsibility: Verify that ``_origin_allowed`` accepts same-origin requests
    and explicit allowlist entries, and rejects others. The full WebSocket
    upgrade flow is exercised by integration tests; here we focus on the
    pure predicate so the security gate is locked down by a unit test.
Edit boundaries: Unit test only — no real WebSocket needed.
Key entry points: ``test_no_origin_allowed`` (native clients),
    ``test_same_origin_allowed``, ``test_unknown_origin_rejected``,
    ``test_explicit_allowlist_admits_origin``.
Risky contracts: The default (allowlist empty) must enforce same-origin only.
Validation: ``uv run pytest -q api/tests/test_terminal_ws_origin.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import api.routes.terminal.ws as ws_module
import pytest


def _make_ws(origin: str | None, host: str = "elb.example.com") -> object:
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    headers["host"] = host
    return SimpleNamespace(headers=headers)


@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "_TERMINAL_WS_ALLOW_ANY_ORIGIN", False, raising=True)


def test_no_origin_allowed() -> None:
    # Non-browser clients (curl, python websockets test helpers) don't send
    # Origin. The ticket flow still gates them.
    assert ws_module._origin_allowed(_make_ws(None)) is True  # type: ignore[arg-type]


def test_same_origin_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "_ALLOWED_ORIGINS", frozenset(), raising=True)
    assert (
        ws_module._origin_allowed(
            _make_ws("https://elb.example.com", host="elb.example.com")
        )
        is True
    )
    # http:// scheme on the same host also accepted (Container App TLS
    # terminator sometimes forwards both depending on the ingress config).
    assert (
        ws_module._origin_allowed(
            _make_ws("http://elb.example.com", host="elb.example.com")
        )
        is True
    )


def test_unknown_origin_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "_ALLOWED_ORIGINS", frozenset(), raising=True)
    assert (
        ws_module._origin_allowed(
            _make_ws("https://evil.example.com", host="elb.example.com")
        )
        is False
    )


def test_explicit_allowlist_admits_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ws_module,
        "_ALLOWED_ORIGINS",
        frozenset({"https://preview.example.com"}),
        raising=True,
    )
    assert (
        ws_module._origin_allowed(
            _make_ws("https://preview.example.com", host="elb.example.com")
        )
        is True
    )
    assert (
        ws_module._origin_allowed(
            _make_ws("https://other.example.com", host="elb.example.com")
        )
        is False
    )


def test_bypass_flag_permits_any(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "_TERMINAL_WS_ALLOW_ANY_ORIGIN", True, raising=True)
    assert (
        ws_module._origin_allowed(_make_ws("https://anything.example", host="x")) is True
    )
