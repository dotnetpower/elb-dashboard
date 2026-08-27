"""Tests for advisory NCBI FTP release metadata.

Responsibility: Verify bounded index parsing, defensive copies, TTL caching,
    retry-after-failure behavior, and release-date comparison.
Edit boundaries: Mock the pooled HTTP client; never contact NCBI from tests.
Key entry points: `test_latest_ftp_releases_parses_and_caches`,
    `test_latest_ftp_releases_does_not_cache_failure`,
    `test_ftp_release_is_newer_compares_snapshot_dates`.
Risky contracts: FTP facts are advisory only and malformed/oversized responses
    must fail without poisoning the process cache.
Validation: `uv run pytest -q api/tests/test_ncbi_releases.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.routes.storage.common import NcbiUnavailable
from api.services import ncbi_releases


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode()


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls = 0

    def get(self, _url: str) -> _Response:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    ncbi_releases._reset_ftp_release_cache()


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    monkeypatch.setattr(
        "api.services.httpx_pool.get_pooled_client",
        lambda *_args, **_kwargs: client,
        raising=True,
    )


def test_latest_ftp_releases_parses_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            _Response(
                [
                    {
                        "dbname": "core_nt",
                        "last-updated": "2026-08-19T00:00:00",
                        "number-of-volumes": 84,
                        "bytes-total": 282_692_127_129,
                        "number-of-sequences": 130_155_243,
                    }
                ]
            )
        ]
    )
    _patch_client(monkeypatch, client)

    first = ncbi_releases.latest_ftp_releases(ttl_seconds=60)
    first["core_nt"]["number_of_volumes"] = 0
    second = ncbi_releases.latest_ftp_releases(ttl_seconds=60)

    assert client.calls == 1
    assert second["core_nt"] == {
        "db_name": "core_nt",
        "last_updated": "2026-08-19T00:00:00",
        "number_of_volumes": 84,
        "bytes_total": 282_692_127_129,
        "number_of_sequences": 130_155_243,
    }


def test_latest_ftp_releases_does_not_cache_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            _Response([], status_code=503),
            _Response(
                [
                    {
                        "dbname": "core_nt",
                        "last-updated": "2026-08-19T00:00:00",
                    }
                ]
            ),
        ]
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(NcbiUnavailable):
        ncbi_releases.latest_ftp_releases(ttl_seconds=60)
    releases = ncbi_releases.latest_ftp_releases(ttl_seconds=60)

    assert client.calls == 2
    assert releases["core_nt"]["last_updated"] == "2026-08-19T00:00:00"


def test_ftp_release_is_newer_compares_snapshot_dates() -> None:
    assert ncbi_releases.ftp_release_is_newer(
        "2026-08-19T00:00:00",
        "2026-07-21-01-05-02",
        "2026-07-21-01-05-02",
    )
    assert not ncbi_releases.ftp_release_is_newer(
        "2026-07-18T00:00:00",
        "2026-07-21-01-05-02",
    )
    assert not ncbi_releases.ftp_release_is_newer(
        "2026-07-21T23:59:59",
        "2026-07-21-01-05-02",
    )
    assert not ncbi_releases.ftp_release_is_newer(
        "not-a-date",
        "2026-07-21-01-05-02",
    )
    assert not ncbi_releases.ftp_release_is_newer(
        "2026-13-32T00:00:00",
        "2026-07-21-01-05-02",
    )
