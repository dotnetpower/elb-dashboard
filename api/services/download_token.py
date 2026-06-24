"""Signed download-URL tokens for result-file links on completion events.

Module summary: lets a Service Bus completion-event consumer download a result
file by URL alone — no bearer token, no interactive ``az`` login — while keeping
the ``api`` sidecar the only thing that ever touches Storage. The token is an
HMAC signature over ``(version, job_id, file_id, expiry)``, NOT a Storage SAS
and NOT a direct Storage URL (charter §9): the signed link still points at the
dashboard's own streaming gateway. The signing key is DERIVED from the shared
``EXEC_TOKEN`` Container Apps secret via domain separation, so no new secret or
Bicep wiring is needed — the ``api`` (verify) and ``worker`` (mint) sidecars
both already carry ``EXEC_TOKEN``.

Responsibility: mint and verify scoped, expiring download tokens.
Edit boundaries: pure crypto/encoding helpers; no FastAPI / Storage / Azure SDK
  imports. The route gate that consumes a token lives in ``api/auth.py``; the
  mint call site lives in ``api/tasks/servicebus/tasks.py``.
Key entry points: ``signing_enabled``, ``mint_download_token``,
  ``verify_download_token``.
Risky contracts: a token authorises exactly one ``(job_id, file_id)`` pair until
  its embedded expiry; the wire format is ``"<version>.<exp>.<sig>"``. Bump
  ``_VERSION`` if the signed message layout ever changes so old tokens fail
  closed instead of being silently misverified.
Validation: ``uv run pytest -q api/tests/test_download_token.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

LOGGER = logging.getLogger(__name__)

# Token wire-format version. Embedded in both the signed message and the token
# string so a future layout change invalidates old tokens (fail closed).
_VERSION = "v1"

# Domain-separation label: the download signing key is HMAC(EXEC_TOKEN, label)
# so reusing EXEC_TOKEN for URL signing never exposes (or collides with) the raw
# exec-channel secret.
_DERIVE_LABEL = b"elb-download-url-v1"

# Kill switch (charter §12a Rule 4 — reversible). Set to "false" to stop minting
# new signed URLs; verification of already-issued tokens is unaffected so a flip
# never breaks links already in flight on the completion topic.
_KILL_ENV = "DOWNLOAD_URL_SIGNED_TOKENS"

# Token lifetime. Completion events are consumed asynchronously (a subscriber may
# pull the topic minutes or hours after the job finishes), so the default is
# generous; the result blob is durably in Storage for the whole window. Scoped to
# one (job_id, file_id), so a leaked link exposes only that single file.
_TTL_ENV = "DOWNLOAD_URL_TTL_SECONDS"
_DEFAULT_TTL_SEC = 7 * 24 * 3600


def _root_key() -> bytes | None:
    """Derive the download signing key from ``EXEC_TOKEN`` (or ``None``).

    Returns ``None`` when ``EXEC_TOKEN`` is absent (e.g. a local dev process
    without the secret); callers treat that as "signing unavailable" and fall
    back to bearer-only auth, so the feature is safely OFF by default.
    """
    raw = os.environ.get("EXEC_TOKEN", "").strip()
    if not raw:
        return None
    return hmac.new(raw.encode("utf-8"), _DERIVE_LABEL, hashlib.sha256).digest()


def signing_enabled() -> bool:
    """True when a signing key is available AND the kill switch is not set off."""
    if os.environ.get(_KILL_ENV, "").strip().lower() == "false":
        return False
    return _root_key() is not None


def _ttl_seconds() -> int:
    raw = os.environ.get(_TTL_ENV, "").strip()
    if not raw:
        return _DEFAULT_TTL_SEC
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TTL_SEC
    return value if value > 0 else _DEFAULT_TTL_SEC


def _signature(key: bytes, job_id: str, file_id: str, exp: int) -> str:
    message = f"{_VERSION}:{job_id}:{file_id}:{exp}".encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def mint_download_token(
    job_id: str, file_id: str, *, ttl_sec: int | None = None
) -> str | None:
    """Mint a signed download token for one ``(job_id, file_id)`` pair.

    Returns ``None`` when signing is unavailable/disabled, so the caller emits a
    bearer-only ``download_url`` unchanged. The returned token is appended to the
    URL as ``?token=...`` and later checked by ``verify_download_token``.
    """
    if not signing_enabled():
        return None
    key = _root_key()
    if key is None:  # pragma: no cover - signing_enabled already checked the key
        return None
    job_id = str(job_id or "")
    file_id = str(file_id or "")
    if not job_id or not file_id:
        return None
    exp = int(time.time()) + (ttl_sec if ttl_sec and ttl_sec > 0 else _ttl_seconds())
    return f"{_VERSION}.{exp}.{_signature(key, job_id, file_id, exp)}"


def verify_download_token(token: str, job_id: str, file_id: str) -> bool:
    """Constant-time verify a token against an expected ``(job_id, file_id)``.

    Independent of the kill switch: an already-issued token keeps working until
    its embedded expiry even if minting was turned off in the meantime. Returns
    ``False`` on any malformed token, expiry, scope mismatch, or missing key.
    """
    key = _root_key()
    if key is None or not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    version, exp_raw, signature = parts
    if version != _VERSION:
        return False
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    expected = _signature(key, str(job_id or ""), str(file_id or ""), exp)
    return hmac.compare_digest(expected, signature)
