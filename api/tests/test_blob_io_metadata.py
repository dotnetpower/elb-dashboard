"""Behaviour tests for the metadata-blob read helpers.

Responsibility: Cover the empty / oversize / range-error edge cases of
``read_metadata_blob_bytes`` so callers (upgrade history, jobstate,
warmup metadata, etc.) get a stable bytes-or-empty contract.
Edit boundaries: Exercises only ``api.services.storage.blob_io``; do not
add network or Azure SDK fixtures here.
Key entry points: ``read_metadata_blob_bytes``.
Risky contracts: A 0-byte append blob causes Azure Storage to return
HTTP 416 InvalidRange — that must collapse to an empty payload, not a
WARNING + propagated exception.
Validation: ``uv run pytest -q api/tests/test_blob_io_metadata.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.storage.blob_io import read_metadata_blob_bytes
from azure.core.exceptions import HttpResponseError


class _FakeDownload:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readall(self) -> bytes:
        return self._payload


class _OkBlobClient:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.calls: list[tuple[int, int | None]] = []

    def download_blob(self, *, offset: int = 0, length: int | None = None) -> _FakeDownload:
        self.calls.append((offset, length))
        return _FakeDownload(self._payload[offset : (offset + length) if length else None])


class _InvalidRangeBlobClient:
    """Simulates the Azure Storage response for downloading a 0-byte blob.

    The SDK raises ``HttpResponseError`` with ``status_code=416`` and
    ``error_code="InvalidRange"`` because no valid byte range exists.
    """

    def __init__(self, *, status_code: int = 416, error_code: str = "InvalidRange") -> None:
        self.status_code = status_code
        self.error_code = error_code

    def download_blob(self, **_kwargs: Any) -> _FakeDownload:
        exc = HttpResponseError(message="The range specified is invalid")
        exc.status_code = self.status_code
        exc.error_code = self.error_code
        raise exc


def test_read_metadata_blob_bytes_returns_payload() -> None:
    client = _OkBlobClient(b"hello world")
    assert read_metadata_blob_bytes(client, max_bytes=64, label="t") == b"hello world"
    # The reader asks for ``max_bytes + 1`` so the over-cap branch is
    # detectable server-side.
    assert client.calls == [(0, 65)]


def test_read_metadata_blob_bytes_collapses_invalid_range_to_empty() -> None:
    """A 0-byte append blob returns 416 InvalidRange for any range request.

    The reader must surface that as empty bytes so callers (upgrade
    history, jobstate, warmup metadata) keep a simple bytes-or-empty
    contract instead of needing to catch HttpResponseError themselves.
    """
    client = _InvalidRangeBlobClient()
    assert read_metadata_blob_bytes(client, label="t") == b""


def test_read_metadata_blob_bytes_only_eats_invalid_range_not_all_416() -> None:
    """Other ``HttpResponseError`` shapes must still propagate so callers
    can react to genuine failures (auth, network, throttling)."""
    client = _InvalidRangeBlobClient(status_code=503, error_code="ServiceUnavailable")
    with pytest.raises(HttpResponseError):
        read_metadata_blob_bytes(client, label="t")


def test_read_metadata_blob_bytes_rejects_oversize_payload() -> None:
    client = _OkBlobClient(b"x" * 10)
    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        read_metadata_blob_bytes(client, max_bytes=5, label="t")
