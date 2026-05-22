"""Tests for NCBI circuit breaker + composite signature in ncbi_catalogue.

Responsibility: Cover the circuit breaker that protects NCBI from runaway
    retries after consecutive 403/5xx, and the composite signature sampler
    that detects updates on later shards.
Edit boundaries: Mock httpx + the snapshot/list helpers; never reach NCBI.
Key entry points: ``test_breaker_opens_after_consecutive_failures``,
    ``test_breaker_closes_after_cooldown``,
    ``test_composite_signature_samples_multiple_shards``,
    ``test_composite_changes_when_any_sampled_shard_rotates``.
Risky contracts: Breaker state is process-local; the reset helper must clear
    both cache and breaker so other tests are not affected.
Validation: ``uv run pytest -q api/tests/test_ncbi_breaker_composite.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from api.routes.storage import common as storage_common
from api.services import ncbi_catalogue


@pytest.fixture(autouse=True)
def _reset() -> None:
    storage_common.reset_ncbi_catalogue_cache()
    ncbi_catalogue._reset_preview_cache()


class _RespOK:
    status_code = 200

    def __init__(self, text: str = "snap", content: bytes = b"") -> None:
        self.text = text
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _Resp500:
    status_code = 500
    text = "boom"
    content = b"boom"

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            "500", request=None, response=httpx.Response(500)  # type: ignore[arg-type]
        )


def test_breaker_opens_after_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tight threshold so we don't loop 5x in tests.
    monkeypatch.setattr(storage_common, "_NCBI_BREAKER_THRESHOLD", 2, raising=True)
    monkeypatch.setattr(storage_common, "_NCBI_BREAKER_COOLDOWN", 9999.0, raising=True)
    storage_common._NCBI_BREAKER_STATE["failures"] = 0
    storage_common._NCBI_BREAKER_STATE["opened_at"] = 0.0

    def _fake_get(_url: str, timeout: float | None = None) -> Any:
        return _Resp500()

    monkeypatch.setattr(httpx, "get", _fake_get)

    # First two failures bump counter, third call should be refused by the
    # breaker without even hitting httpx.get.
    with pytest.raises(storage_common.NcbiUnavailable):
        storage_common._resolve_latest_dir()
    with pytest.raises(storage_common.NcbiUnavailable):
        storage_common._resolve_latest_dir()

    # Patch httpx.get to raise loudly so we know the third call did NOT
    # actually hit the network.
    def _should_not_call(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("breaker did not open — httpx.get was called")

    monkeypatch.setattr(httpx, "get", _should_not_call)
    with pytest.raises(storage_common.NcbiUnavailable):
        storage_common._resolve_latest_dir()


def test_breaker_closes_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_common, "_NCBI_BREAKER_THRESHOLD", 1, raising=True)
    monkeypatch.setattr(storage_common, "_NCBI_BREAKER_COOLDOWN", 0.0, raising=True)
    storage_common._NCBI_BREAKER_STATE["failures"] = 0
    storage_common._NCBI_BREAKER_STATE["opened_at"] = 0.0

    def _fake_get(_url: str, timeout: float | None = None) -> Any:
        return _Resp500()

    monkeypatch.setattr(httpx, "get", _fake_get)
    with pytest.raises(storage_common.NcbiUnavailable):
        storage_common._resolve_latest_dir()
    # Cooldown is 0 so the next probe should close + retry.
    assert storage_common._breaker_open() is False

    def _ok(_url: str, timeout: float | None = None) -> Any:
        return _RespOK(text="2026-05-21-01-05-02")

    monkeypatch.setattr(httpx, "get", _ok)
    assert storage_common._resolve_latest_dir() == "2026-05-21-01-05-02"


def test_composite_signature_samples_multiple_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For an 8-volume DB the composite should incorporate every sampled
    shard's ETag — not just the first."""
    snapshot = "2026-05-21-01-05-02"
    db = "core_nt"
    keys = [f"{snapshot}/core_nt.{i:03d}.tar.gz.md5" for i in range(8)]
    keys += [f"{snapshot}/core_nt.{i:03d}.nhr" for i in range(8)]
    monkeypatch.setattr(ncbi_catalogue, "_resolve_latest_dir", lambda: snapshot)
    monkeypatch.setattr(ncbi_catalogue, "_list_keys", lambda *_a, **_kw: keys)

    class _Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

        def head(self, url: str, timeout: float | None = None) -> Any:
            # Distinct ETag per md5; static ETag for .nhr.
            etag = "stable"
            for i in range(8):
                if f"core_nt.{i:03d}.tar.gz.md5" in url:
                    etag = f"md5-{i}"
                    break
            return type(
                "_R",
                (),
                {
                    "status_code": 200,
                    "headers": {
                        "ETag": f'"{etag}"',
                        "Last-Modified": "Thu, 21 May 2026 03:00:00 GMT",
                        "Content-Length": "1024",
                    },
                },
            )

    monkeypatch.setattr(
        ncbi_catalogue,
        "httpx",
        type("_h", (), {"Client": _Client, "HTTPError": httpx.HTTPError}),
        raising=True,
    )
    summary = ncbi_catalogue.preview_database(db)
    assert summary["composite_signature"]
    assert summary["signature_sample_count"] >= 2


def test_composite_changes_when_any_sampled_shard_rotates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = "2026-05-21-01-05-02"
    db = "core_nt"
    keys = [f"{snapshot}/core_nt.{i:03d}.tar.gz.md5" for i in range(8)]
    monkeypatch.setattr(ncbi_catalogue, "_resolve_latest_dir", lambda: snapshot)
    monkeypatch.setattr(ncbi_catalogue, "_list_keys", lambda *_a, **_kw: keys)
    state = {"rotate": False}

    class _Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

        def head(self, url: str, timeout: float | None = None) -> Any:
            etag = "stable"
            for i in range(8):
                if f"core_nt.{i:03d}.tar.gz.md5" in url:
                    # Rotate only the LAST shard between calls.
                    if i == 7 and state["rotate"]:
                        etag = "md5-7-rotated"
                    else:
                        etag = f"md5-{i}"
                    break
            return type(
                "_R",
                (),
                {
                    "status_code": 200,
                    "headers": {
                        "ETag": f'"{etag}"',
                        "Last-Modified": "Thu, 21 May 2026 03:00:00 GMT",
                        "Content-Length": "1024",
                    },
                },
            )

    monkeypatch.setattr(
        ncbi_catalogue,
        "httpx",
        type("_h", (), {"Client": _Client, "HTTPError": httpx.HTTPError}),
        raising=True,
    )
    before = ncbi_catalogue.preview_database(db)["composite_signature"]
    ncbi_catalogue._reset_preview_cache()
    state["rotate"] = True
    after = ncbi_catalogue.preview_database(db)["composite_signature"]
    assert before != after, "rotating any sampled shard's md5 must change composite"
