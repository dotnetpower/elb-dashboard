"""Tests for the BLAST submit error reporting hardening (critique #7).

Responsibility: Pin the sanitisation contract for HTTPException
    ``detail.message`` payloads in the BLAST submit route so a future
    refactor cannot accidentally leak Storage SAS tokens / URLs via
    a raw ``str(exc)``.
Edit boundaries: Unit-level only \u2014 calls ``_safe_exc_message``
    directly. Route-integration coverage lives in
    ``test_blast_submit_route_options.py``.
Key entry points: ``_safe_exc_message``.
Risky contracts: The sanitiser is the canonical
    ``api.services.sanitise.sanitise``; this test will need updating
    if a new redaction is added there.
Validation: ``uv run pytest -q api/tests/test_blast_submit_error_sanitisation.py``.
"""

from __future__ import annotations

import pytest
from api.routes.blast.submit import _EXCEPTION_DETAIL_MAX_CHARS, _safe_exc_message


def test_safe_exc_message_redacts_sas_query_string() -> None:
    """Critique #7: raw Azure SDK error strings can carry a full SAS
    query string. The helper must hand it off to ``sanitise`` which
    rewrites it to ``?<sas-redacted>``.
    """
    exc = RuntimeError(
        "AuthenticationFailed: Server failed to authenticate the request. "
        "URL: https://elb.blob.core.windows.net/queries/q1.fa"
        "?sv=2024-08-04&ss=b&srt=co&sp=rwdlacx&se=2026-12-31T23:59:59Z&sig=AbCdEf%2BGhIj"
    )
    out = _safe_exc_message(exc)
    assert "sig=AbCdEf" not in out
    # sanitise() rewrites either to "?<sas-redacted>" or to "sig=<redacted>".
    assert "sas-redacted" in out or "sig=<redacted>" in out


def test_safe_exc_message_redacts_standalone_sig_param() -> None:
    """A ``?sig=…`` segment by itself (no other SAS keys) must also be
    redacted \u2014 covered by the ``_SAS_SIG_RE`` fallback."""
    exc = RuntimeError(
        "Storage upload failed for https://elb.blob.core.windows.net/x?sig=AbCdEf"
    )
    out = _safe_exc_message(exc)
    assert "sig=AbCdEf" not in out
    assert "sig=<redacted>" in out


def test_safe_exc_message_truncates_to_max_chars() -> None:
    """The truncation cap is independent of the sanitisation pass and
    must apply AFTER it so a sanitised but lengthy message still fits
    in the documented envelope.
    """
    long_text = "A" * (_EXCEPTION_DETAIL_MAX_CHARS * 4)
    exc = RuntimeError(long_text)
    out = _safe_exc_message(exc)
    assert len(out) <= _EXCEPTION_DETAIL_MAX_CHARS


def test_safe_exc_message_handles_empty_and_unicode() -> None:
    """The helper must not raise for an empty exception body or a
    unicode message, and must return a regular ``str``.

    Critique-round-1 M8: an empty ``str(exc)`` (e.g. bare
    ``RuntimeError()``) used to surface as an empty body; the helper
    now falls back to ``repr(exc)`` so the user at least sees the
    exception class name.
    """
    out_empty = _safe_exc_message(RuntimeError(""))
    assert out_empty != ""
    assert "RuntimeError" in out_empty
    out = _safe_exc_message(RuntimeError("\ub300\uc18c\ubb38\uc790\uc5f4 OK"))
    assert isinstance(out, str)
    assert "OK" in out


def test_safe_exc_message_redacts_subscription_id() -> None:
    """A subscription id in the raw exception text must be masked by
    ``sanitise(..., mask_subscription_ids=True)`` (its default)."""
    sub = "12345678-1234-1234-1234-123456789012"
    exc = RuntimeError(f"AuthorizationFailed at /subscriptions/{sub}/resourceGroups/rg-elb")
    out = _safe_exc_message(exc)
    assert sub not in out


@pytest.mark.parametrize(
    "exc_text",
    [
        "plain error with no secrets",
        "AKS provisioning failed: managed cluster not found",
        "Connection reset by peer",
    ],
)
def test_safe_exc_message_passes_clean_text_through(exc_text: str) -> None:
    """Strings with no secrets should round-trip without mangling so
    the user still gets a useful diagnostic."""
    out = _safe_exc_message(RuntimeError(exc_text))
    # The sanitiser may rewrite some tokens, but ordinary words must survive.
    for word in exc_text.split():
        if "/" in word or "://" in word or "=" in word:
            # URL / kv-pair fragments may be rewritten \u2014 skip.
            continue
        assert word in out


def test_safe_exc_message_redacts_azure_sdk_style_error() -> None:
    """Round-2 R2-M5: the realistic shape we want to defend against is
    an Azure SDK ``HttpResponseError`` whose ``str()`` includes the
    full request URL, all SAS query keys, AND a correlation id. Pin
    the redaction so a regression in the sanitiser is caught here.
    """
    azure_style = (
        "(AuthorizationFailure) This request is not authorized to perform "
        "this operation.\n"
        "RequestId:abc-123-correlation\n"
        "Time:2026-05-29T10:00:00Z\n"
        "ErrorCode:AuthorizationFailure\n"
        "Url: https://elbstg01.blob.core.windows.net/queries/q.fa"
        "?sv=2024-08-04&ss=b&srt=co&sp=rwdlacx&se=2026-12-31T00:00:00Z"
        "&sig=A1b2C3d4%2BE5fG6h7"
    )
    out = _safe_exc_message(RuntimeError(azure_style))
    # The signature must be gone\u2026
    assert "A1b2C3d4" not in out
    # \u2026and the request id stays (it is not a secret) so a support
    # case can be correlated.
    assert "abc-123-correlation" in out
