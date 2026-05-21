"""Regression tests for security-audit items #4 (tid) + #8 (storage cross-check).

Responsibility: Cover the two related defence-in-depth gates that landed
together on 2026-05-22 — explicit `tid` claim verification in the MSAL
validator, and the JobState storage-account cross-check on every
job-bound BLAST results route.
Edit boundaries: Keep these tests focused on the security invariants;
behavioural assertions for the BLAST routes themselves belong in
``test_blast_results_routes.py``.
Key entry points: ``test_token_with_mismatched_tid_is_rejected``,
``test_resolve_job_storage_account_rejects_cross_account``,
``test_resolve_job_storage_account_normalises_case``,
``test_resolve_job_storage_account_falls_back_when_unrecorded``.
Risky contracts: The configured ``AZURE_TENANT_ID`` is the only tenant
whose tokens the api will trust; supplied ``storage_account`` must match
the JobState row or the request must be refused without echoing the
recorded value.
Validation: ``uv run pytest -q api/tests/test_security_audit_4_8.py``.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# #4 — explicit `tid` claim verification
# ---------------------------------------------------------------------------
_TEST_TENANT_ID = "11111111-2222-3333-4444-555555555555"
_TEST_API_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OTHER_TENANT_ID = "99999999-9999-9999-9999-999999999999"


def _make_token(claims: dict[str, Any], key: str) -> str:
    """Mint an HS256 token for the test (the real validator uses RS256
    against AAD JWKS; we monkeypatch the signing-key lookup so the
    algorithm choice does not matter here)."""
    return jwt.encode(claims, key, algorithm="HS256")


@pytest.fixture()
def _auth_env(monkeypatch: pytest.MonkeyPatch):
    from api import auth as auth_mod

    auth_mod.reset_caches()
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", _TEST_TENANT_ID)
    monkeypatch.setenv("API_CLIENT_ID", _TEST_API_CLIENT_ID)
    yield
    auth_mod.reset_caches()


def _patch_jwt(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Force the validator to skip the JWKS roundtrip and accept HS256."""
    from api import auth as auth_mod

    class _StubSigningKey:
        def __init__(self, k: str) -> None:
            self.key = k

    class _StubJwks:
        def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
            return _StubSigningKey(key)

    monkeypatch.setattr(auth_mod, "_get_jwks_client", lambda _tid: _StubJwks())

    real_decode = jwt.decode

    def _decode(token: str, signing_key: str, **kwargs: Any) -> Any:
        # Mirror the real decode but force HS256 so the test does not need
        # an RSA keypair.
        kwargs["algorithms"] = ["HS256"]
        return real_decode(token, signing_key, **kwargs)

    monkeypatch.setattr(auth_mod.jwt, "decode", _decode)


def test_token_with_matching_tid_is_accepted(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: a correctly-issued token must keep working."""
    from api.auth import _validate_token

    secret = "shared-secret-for-test"
    _patch_jwt(monkeypatch, secret)

    now = int(time.time())
    token = _make_token(
        {
            "iss": f"https://login.microsoftonline.com/{_TEST_TENANT_ID}/v2.0",
            "aud": _TEST_API_CLIENT_ID,
            "tid": _TEST_TENANT_ID,
            "oid": "alice-oid",
            "upn": "alice@example.com",
            "iat": now,
            "exp": now + 300,
        },
        secret,
    )
    identity = _validate_token(token)
    assert identity.object_id == "alice-oid"
    assert identity.tenant_id == _TEST_TENANT_ID


def test_token_with_mismatched_tid_is_rejected(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token whose ``tid`` claim does not match ``AZURE_TENANT_ID``
    must be refused with 401, even if every other claim looks valid.

    Without this gate, a regression that broadens the issuer list (e.g.
    by accidentally accepting the ``common`` endpoint) would silently
    accept cross-tenant tokens. The explicit ``tid`` check makes the
    boundary a single line of code, not a multi-line issuer list.
    """
    from api.auth import _validate_token

    secret = "shared-secret-for-test"
    _patch_jwt(monkeypatch, secret)

    now = int(time.time())
    # The issuer URL still lies about being our tenant (this is the case
    # we are defending against — a permissive issuer accept list), but
    # the ``tid`` claim correctly identifies the *real* originating
    # tenant. The explicit ``tid`` check must catch this.
    token = _make_token(
        {
            "iss": f"https://login.microsoftonline.com/{_TEST_TENANT_ID}/v2.0",
            "aud": _TEST_API_CLIENT_ID,
            "tid": _OTHER_TENANT_ID,
            "oid": "mallory-oid",
            "iat": now,
            "exp": now + 300,
        },
        secret,
    )
    with pytest.raises(HTTPException) as excinfo:
        _validate_token(token)
    assert excinfo.value.status_code == 401
    assert "tenant" in str(excinfo.value.detail).lower()


def test_token_missing_tid_is_rejected(
    _auth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token that omits the ``tid`` claim entirely cannot be trusted —
    AAD always issues it, so its absence indicates a tampered token or
    a non-AAD issuer."""
    from api.auth import _validate_token

    secret = "shared-secret-for-test"
    _patch_jwt(monkeypatch, secret)

    now = int(time.time())
    token = _make_token(
        {
            "iss": f"https://login.microsoftonline.com/{_TEST_TENANT_ID}/v2.0",
            "aud": _TEST_API_CLIENT_ID,
            "oid": "alice-oid",
            "iat": now,
            "exp": now + 300,
        },
        secret,
    )
    with pytest.raises(HTTPException) as excinfo:
        _validate_token(token)
    assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# #8 — storage_account ↔ JobState cross-check
# ---------------------------------------------------------------------------
def _state(storage_account: str | None) -> Any:
    """Return a stub JobState summary row with the given storage_account."""
    from types import SimpleNamespace

    return SimpleNamespace(storage_account=storage_account)


def test_resolve_job_storage_account_accepts_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline: the supplied account matches the JobState row."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.services.blast_job_state import _resolve_job_storage_account

    class _Repo:
        def get_summary(self, _job_id: str) -> Any:
            return _state("stelb01")

    with patch("api.services.state_repo.JobStateRepository", lambda: _Repo()):
        assert _resolve_job_storage_account("job-1", "stelb01") == "stelb01"


def test_resolve_job_storage_account_normalises_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage account names are case-insensitive in Azure; the gate
    must not reject the caller for using a different casing than the
    submit-time value, but it must return the *recorded* casing so
    downstream code is byte-identical regardless of caller input."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.services.blast_job_state import _resolve_job_storage_account

    class _Repo:
        def get_summary(self, _job_id: str) -> Any:
            return _state("stelb01")

    with patch("api.services.state_repo.JobStateRepository", lambda: _Repo()):
        assert _resolve_job_storage_account("job-1", "STELB01") == "stelb01"
        assert _resolve_job_storage_account("job-1", "  stelb01  ") == "stelb01"


def test_resolve_job_storage_account_rejects_cross_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supplied account differs from the recorded one — must 403, and
    must NOT echo the recorded value (that would leak the correct
    account name to anyone probing job_ids)."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.services.blast_job_state import _resolve_job_storage_account

    class _Repo:
        def get_summary(self, _job_id: str) -> Any:
            return _state("stelb-confidential")

    with patch("api.services.state_repo.JobStateRepository", lambda: _Repo()):
        with pytest.raises(HTTPException) as excinfo:
            _resolve_job_storage_account("job-1", "stelb-attacker")
    assert excinfo.value.status_code == 403
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "cross_account_mismatch"
    # Critical: the recorded value must NOT appear in the response.
    assert "stelb-confidential" not in str(detail)


def test_resolve_job_storage_account_falls_back_when_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy / external-sync rows have no recorded storage_account.
    The gate must degrade open (return supplied) — a hard failure here
    would break the legacy job listing for everyone."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.services.blast_job_state import _resolve_job_storage_account

    class _Repo:
        def get_summary(self, _job_id: str) -> Any:
            return _state("")  # row exists but the field is empty

    with patch("api.services.state_repo.JobStateRepository", lambda: _Repo()):
        assert _resolve_job_storage_account("job-1", "any-supplied") == "any-supplied"


def test_resolve_job_storage_account_fails_closed_when_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state-repo outage must NOT be exploitable as a cross-account
    bypass. Without dev bypass set, the helper must raise 503."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.services.blast_job_state import _resolve_job_storage_account

    class _ExplodingRepo:
        def get_summary(self, _job_id: str) -> Any:
            raise RuntimeError("table not found")

    with patch("api.services.state_repo.JobStateRepository", lambda: _ExplodingRepo()):
        with pytest.raises(HTTPException) as excinfo:
            _resolve_job_storage_account("job-1", "stelb-supplied")
    assert excinfo.value.status_code == 503
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "auth_lookup_unavailable"


def test_resolve_job_storage_account_degrades_open_in_dev_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under AUTH_DEV_BYPASS the dev loop has no real state backend; the
    helper degrades open so local development is not blocked."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.services.blast_job_state import _resolve_job_storage_account

    class _ExplodingRepo:
        def get_summary(self, _job_id: str) -> Any:
            raise RuntimeError("AZURE_TABLE_ENDPOINT not set")

    with patch("api.services.state_repo.JobStateRepository", lambda: _ExplodingRepo()):
        assert _resolve_job_storage_account("job-1", "stelb-dev") == "stelb-dev"


def test_resolve_job_storage_account_empty_supplied_returns_immediately() -> None:
    """If the caller did not supply storage_account at all (optional on a
    few routes), the helper must not raise — it should return the empty
    string and let the route's own validation handle it."""
    from api.services.blast_job_state import _resolve_job_storage_account

    assert _resolve_job_storage_account("job-1", "") == ""


# ---------------------------------------------------------------------------
# Hardening: end-to-end wiring via TestClient — confirms each job-bound
# results route actually calls _resolve_job_storage_account (catches a
# future refactor that drops the helper from a route handler).
# ---------------------------------------------------------------------------
@pytest.fixture()
def _route_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


def _install_cross_account_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """JobState says ``stelb-confidential``; anything else must 403."""
    from types import SimpleNamespace

    class _Repo:
        def get(self, _job_id: str) -> Any:
            # _ensure_job_read_allowed uses .get() — return a row owned
            # by the dev-bypass synthetic identity so the owner gate
            # passes; we want to exercise the storage cross-check, not
            # the owner check.
            return SimpleNamespace(
                owner_oid="00000000-0000-0000-0000-000000000000",
                storage_account="stelb-confidential",
            )

        def get_summary(self, _job_id: str) -> Any:
            return SimpleNamespace(storage_account="stelb-confidential")

    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        lambda: _Repo(),
        raising=True,
    )


@pytest.mark.parametrize(
    "route_path",
    [
        "/api/blast/jobs/test-job/results/aggregate",
        "/api/blast/jobs/test-job/results/download?blob_name=test-job/x.txt",
        "/api/blast/jobs/test-job/results/export",
        "/api/blast/jobs/test-job/results/alignments",
        "/api/blast/jobs/test-job/results/taxonomy",
    ],
)
def test_each_job_bound_results_route_rejects_cross_account(
    _route_client, monkeypatch: pytest.MonkeyPatch, route_path: str
) -> None:
    """End-to-end: a real HTTP GET with a mismatched storage_account on
    every job-bound results route must return 403 cross_account_mismatch
    BEFORE the Storage SDK is called. If a future refactor drops the
    _resolve_job_storage_account call from any of these handlers, the
    parametrised case for that route fails."""
    _install_cross_account_repo(monkeypatch)
    sep = "&" if "?" in route_path else "?"
    r = _route_client.get(f"{route_path}{sep}storage_account=stelb-attacker")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("code") == "cross_account_mismatch", body
    # Critical: the recorded account name must NOT appear in the body.
    assert "stelb-confidential" not in r.text
