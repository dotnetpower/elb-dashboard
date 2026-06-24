"""Tests for the signed download-URL token service.

Responsibility: cover mint/verify roundtrip, expiry, scope binding, tampering,
  the kill switch, and the missing-key (signing-off) default.
Edit boundaries: test-only; exercises ``api.services.download_token`` directly.
Key entry points: the test functions below.
Risky contracts: a token must authorise exactly one ``(job_id, file_id)`` and
  must fail closed on any mismatch / expiry / missing key.
Validation: ``uv run pytest -q api/tests/test_download_token.py``.
"""

from __future__ import annotations

import time

import pytest
from api.services import download_token as dt


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_TOKEN", "test-exec-token-value")
    monkeypatch.delenv("DOWNLOAD_URL_SIGNED_TOKENS", raising=False)
    monkeypatch.delenv("DOWNLOAD_URL_TTL_SECONDS", raising=False)


def test_mint_and_verify_roundtrip() -> None:
    token = dt.mint_download_token("abc123def456", "merged_results.out.gz")
    assert token is not None
    assert dt.verify_download_token(token, "abc123def456", "merged_results.out.gz")


def test_verify_rejects_wrong_file_id() -> None:
    token = dt.mint_download_token("abc123def456", "result-001")
    assert token is not None
    assert not dt.verify_download_token(token, "abc123def456", "result-002")


def test_verify_rejects_wrong_job_id() -> None:
    token = dt.mint_download_token("abc123def456", "result-001")
    assert token is not None
    assert not dt.verify_download_token(token, "deadbeef0001", "result-001")


def test_verify_rejects_tampered_signature() -> None:
    token = dt.mint_download_token("abc123def456", "result-001")
    assert token is not None
    version, exp, sig = token.split(".")
    tampered = f"{version}.{exp}.{sig[:-1]}{'A' if sig[-1] != 'A' else 'B'}"
    assert not dt.verify_download_token(tampered, "abc123def456", "result-001")


def test_verify_rejects_expired_token() -> None:
    token = dt.mint_download_token("abc123def456", "result-001", ttl_sec=1)
    assert token is not None
    # Forge an already-expired token deterministically by rewriting the exp.
    version, _exp, _sig = token.split(".")
    key = dt._root_key()
    assert key is not None
    past = int(time.time()) - 10
    expired = f"{version}.{past}.{dt._signature(key, 'abc123def456', 'result-001', past)}"
    assert not dt.verify_download_token(expired, "abc123def456", "result-001")


def test_verify_rejects_malformed_token() -> None:
    assert not dt.verify_download_token("", "j", "f")
    assert not dt.verify_download_token("garbage", "j", "f")
    assert not dt.verify_download_token("v1.notanint.sig", "j", "f")
    assert not dt.verify_download_token("v9.123.sig", "j", "f")


def test_signing_disabled_without_exec_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXEC_TOKEN", raising=False)
    assert dt.signing_enabled() is False
    assert dt.mint_download_token("abc123def456", "result-001") is None


def test_kill_switch_stops_minting_but_not_verifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = dt.mint_download_token("abc123def456", "result-001")
    assert token is not None
    monkeypatch.setenv("DOWNLOAD_URL_SIGNED_TOKENS", "false")
    assert dt.signing_enabled() is False
    assert dt.mint_download_token("abc123def456", "result-001") is None
    # Already-issued tokens keep working so in-flight links never break.
    assert dt.verify_download_token(token, "abc123def456", "result-001")


def test_verify_independent_of_key_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    token = dt.mint_download_token("abc123def456", "result-001")
    assert token is not None
    monkeypatch.setenv("EXEC_TOKEN", "a-different-exec-token")
    # Rotating EXEC_TOKEN invalidates old tokens (fail closed).
    assert not dt.verify_download_token(token, "abc123def456", "result-001")


def test_custom_ttl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNLOAD_URL_TTL_SECONDS", "60")
    token = dt.mint_download_token("abc123def456", "result-001")
    assert token is not None
    _version, exp, _sig = token.split(".")
    delta = int(exp) - int(time.time())
    assert 50 <= delta <= 60
