"""Tests for the `caller_hash` field on the request-completion log line.

Responsibility: Verify `RequestIdMiddleware` now stamps every completion
log line (`req rid=...` and `req_failed rid=...`) with a redacted
`caller_hash=<sha256-prefix>` token, per audit P3 #26, and that the raw
`oid` / `upn` claim from the bearer token NEVER appears in the captured
message.
Edit boundaries: Unit-test only — exercises the middleware via Starlette's
in-memory TestClient. No real Azure / network calls.
Key entry points: `_make_bearer`, `test_caller_hash_present_for_anonymous_request`,
`test_caller_hash_present_for_authenticated_request`,
`test_caller_hash_redacts_oid`,
`test_caller_hash_present_on_4xx`,
`test_caller_hash_present_on_5xx_failure`,
`test_decode_jwt_oid_extracts_oid_claim`,
`test_decode_jwt_oid_falls_back_to_sub_claim`,
`test_decode_jwt_oid_returns_none_for_missing_bearer`.
Risky contracts: The `caller_hash=` token must be a `redact_oid()` output —
never the raw `oid` / `sub` / `upn` claim. Tests assert the raw value
does not appear in `caplog` output.
Validation: `uv run pytest -q api/tests/test_caller_hash_logging.py`.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest
from api.app.jwt_utils import _decode_jwt_oid
from api.app.middleware import RequestIdMiddleware
from api.services.sanitise import redact_oid
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

MIDDLEWARE_LOGGER = "api.app.middleware"


def _make_bearer(claims: dict[str, object]) -> str:
    """Forge a non-signed JWT carrying ``claims`` in the payload.

    Tests need a token the middleware's best-effort base64 decoder can
    parse; signature is irrelevant (the inspector path never verifies).
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8"))
        .rstrip(b"=")
        .decode()
    )
    return f"Bearer {header}.{payload}."


@pytest.fixture()
def app_with_middleware() -> FastAPI:
    """Minimal FastAPI app with `RequestIdMiddleware` mounted.

    Avoids spinning up `api.main:create_app()` so the test is fast and
    has no side effects on other modules' env state.
    """
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/forbid")
    def _forbid() -> None:
        raise HTTPException(status_code=403, detail="nope")

    @app.get("/boom")
    def _boom() -> None:
        raise RuntimeError("kaboom")

    return app


@pytest.fixture()
def client(app_with_middleware: FastAPI) -> TestClient:
    return TestClient(app_with_middleware, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Completion log line — happy path.
# ---------------------------------------------------------------------------


def test_caller_hash_present_for_anonymous_request(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    """No bearer → `caller_hash=None`. Token must still be present so a
    log shipper / KQL parser does not have to special-case the
    anonymous row."""
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    response = client.get("/ping")
    assert response.status_code == 200
    msg = _completion_message(caplog)
    assert "caller_hash=None" in msg, f"expected caller_hash token, got: {msg!r}"


def test_caller_hash_present_for_authenticated_request(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    raw_oid = "00000000-aaaa-bbbb-cccc-111111111111"
    expected_hash = redact_oid(raw_oid)
    bearer = _make_bearer({"oid": raw_oid, "upn": "alice@contoso.com"})

    response = client.get("/ping", headers={"authorization": bearer})
    assert response.status_code == 200

    msg = _completion_message(caplog)
    assert f"caller_hash={expected_hash}" in msg


def test_caller_hash_redacts_oid(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    """Raw `oid` / `upn` claim must NEVER appear in the completion line."""
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    raw_oid = "11111111-2222-3333-4444-555555555555"
    raw_upn = "bob@contoso.com"
    bearer = _make_bearer({"oid": raw_oid, "upn": raw_upn})

    response = client.get("/ping", headers={"authorization": bearer})
    assert response.status_code == 200

    msg = _completion_message(caplog)
    assert raw_oid not in msg, "raw oid leaked into completion line"
    assert raw_upn not in msg, "raw upn leaked into completion line"


# ---------------------------------------------------------------------------
# Failure paths — 4xx (handled HTTPException) and 5xx (unhandled raise).
# ---------------------------------------------------------------------------


def test_caller_hash_present_on_4xx(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    """A handled 4xx (e.g. HTTPException) routes through the success path
    of the middleware — the `req rid=... status=403 ...` line must still
    carry `caller_hash`."""
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    raw_oid = "22222222-aaaa-bbbb-cccc-333333333333"
    expected_hash = redact_oid(raw_oid)
    bearer = _make_bearer({"oid": raw_oid})

    response = client.get("/forbid", headers={"authorization": bearer})
    assert response.status_code == 403

    msg = _completion_message(caplog)
    assert "status=403" in msg
    assert f"caller_hash={expected_hash}" in msg


def test_caller_hash_present_on_5xx_failure(
    caplog: pytest.LogCaptureFixture, client: TestClient
) -> None:
    """An unhandled exception routes through the `req_failed rid=...`
    branch of the middleware — that line must also carry `caller_hash`."""
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    raw_oid = "33333333-aaaa-bbbb-cccc-444444444444"
    expected_hash = redact_oid(raw_oid)
    bearer = _make_bearer({"oid": raw_oid})

    # raise_server_exceptions=False on the fixture lets the middleware run
    # to completion; the test client returns 500.
    response = client.get("/boom", headers={"authorization": bearer})
    assert response.status_code == 500

    # The failure path emits `req_failed rid=...`. Find that line
    # explicitly so we don't get the success-path string by accident.
    failure_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == MIDDLEWARE_LOGGER and "req_failed" in r.getMessage()
    ]
    assert failure_lines, "expected a `req_failed` line from RequestIdMiddleware"
    assert any(f"caller_hash={expected_hash}" in line for line in failure_lines)
    # Raw oid must not leak in the failure path either.
    assert all(raw_oid not in line for line in failure_lines)


# ---------------------------------------------------------------------------
# _decode_jwt_oid unit coverage.
# ---------------------------------------------------------------------------


def test_decode_jwt_oid_extracts_oid_claim() -> None:
    bearer = _make_bearer({"oid": "abc-123"})
    assert _decode_jwt_oid(bearer) == "abc-123"


def test_decode_jwt_oid_falls_back_to_sub_claim() -> None:
    bearer = _make_bearer({"sub": "sub-456"})
    assert _decode_jwt_oid(bearer) == "sub-456"


def test_decode_jwt_oid_returns_none_for_missing_bearer() -> None:
    assert _decode_jwt_oid(None) is None
    assert _decode_jwt_oid("") is None
    assert _decode_jwt_oid("Basic abcdef") is None


def test_decode_jwt_oid_returns_none_for_malformed_token() -> None:
    """A malformed bearer (no `.`) must not raise — the middleware path
    relies on the helper degrading gracefully to `None`."""
    assert _decode_jwt_oid("Bearer not-a-jwt") is None
    assert _decode_jwt_oid("Bearer header.not-base64") is None


def test_decode_jwt_oid_truncates_long_values() -> None:
    """Mirror the 128-char cap in `_decode_jwt_upn` so a hostile token
    cannot blow out the log line."""
    bearer = _make_bearer({"oid": "x" * 500})
    extracted = _decode_jwt_oid(bearer)
    assert extracted is not None and len(extracted) == 128


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completion_message(caplog: pytest.LogCaptureFixture) -> str:
    """Return the most recent `req rid=...` line from the middleware logger.

    Filters out the `req_failed` line so the success-path tests do not
    accidentally match the failure path.
    """
    matches = [
        r.getMessage()
        for r in caplog.records
        if r.name == MIDDLEWARE_LOGGER
        and r.getMessage().startswith("req rid=")
    ]
    assert matches, (
        "expected at least one `req rid=...` completion line from RequestIdMiddleware"
    )
    return matches[-1]
