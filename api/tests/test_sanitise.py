"""Tests for output sanitisation.

Responsibility: Tests for output sanitisation
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_masks_sas_query_string`, `test_masks_bearer_token`,
`test_masks_account_key`, `test_masks_guids_when_requested`, `test_returns_empty_for_none`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_sanitise.py`.
"""

from __future__ import annotations

from api.services.sanitise import sanitise


def test_masks_sas_query_string() -> None:
    url = "https://stgelb.blob.core.windows.net/results/file?sv=2024-01-01&sig=ABCDEFGHIJKLMNOPQ&se=2030-01-01"
    out = sanitise(url, mask_subscription_ids=False)
    assert "sig=" not in out
    assert "<sas-redacted>" in out


def test_masks_bearer_token() -> None:
    text = "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.payloadhere"
    out = sanitise(text, mask_subscription_ids=False)
    assert "<redacted>" in out
    assert "eyJ0" not in out


def test_masks_account_key() -> None:
    text = 'AccountKey="abcdefghijklmnopqrstuvwxyz1234567890=="'
    out = sanitise(text, mask_subscription_ids=False)
    assert "abcdefghij" not in out
    assert "<redacted>" in out


def test_masks_guids_when_requested() -> None:
    text = "subscription 11111111-2222-3333-4444-555555555555 ok"
    masked = sanitise(text, mask_subscription_ids=True)
    full = sanitise(text, mask_subscription_ids=False)
    assert "11111111…" in masked
    assert "11111111-2222-3333-4444-555555555555" in full


def test_returns_empty_for_none() -> None:
    assert sanitise(None) == ""


def test_strips_ansi_csi_color_codes() -> None:
    # elastic-blast / az / azcopy colour their stdout. The codes must not
    # reach the JSON state, append-blob log artefacts, or the dashboard UI.
    text = "\x1b[33m[parallel-prep] running 4 azcopy checks concurrently\x1b[0m"
    out = sanitise(text, mask_subscription_ids=False)
    assert out == "[parallel-prep] running 4 azcopy checks concurrently"
    assert "\x1b" not in out


def test_strips_ansi_with_cursor_and_erase_sequences() -> None:
    text = "before\x1b[2K\x1b[1Gmiddle\x1b[K\x1b[31merror\x1b[0mafter"
    out = sanitise(text, mask_subscription_ids=False)
    assert out == "beforemiddleerrorafter"
