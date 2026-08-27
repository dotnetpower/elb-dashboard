"""Current NCBI FTP release metadata for cloud-mirror lag detection.

Responsibility: Read and cache NCBI's small official BLAST database metadata
    index, normalize per-database release facts, and compare release dates with
    available cloud snapshot markers.
Edit boundaries: Read-only NCBI FTP metadata access and pure date comparison;
    cloud object listing, database copies, HTTP response shaping, and UI state
    remain in their owning modules.
Key entry points: `latest_ftp_releases`, `ftp_release_is_newer`.
Risky contracts: The fetch URL is fixed (never caller-controlled), response
    bytes and entry count are capped, failures are not cached, and callers must
    treat this source as advisory because prepare-db copies from the raw-file
    cloud mirror.
Validation: `uv run pytest -q api/tests/test_ncbi_releases.py`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date
from typing import Any, TypedDict

import httpx

from api.routes.storage.common import NcbiAccessDenied, NcbiUnavailable

_FTP_RELEASE_INDEX_URL = "https://ftp.ncbi.nlm.nih.gov/blast/db/v5/blastdb-metadata-1-1.json"
_FTP_RELEASE_CACHE_TTL_SECONDS = float(os.environ.get("NCBI_FTP_RELEASE_CACHE_TTL", "1800.0"))
_FTP_RELEASE_HTTP_TIMEOUT_SECONDS = float(os.environ.get("NCBI_FTP_RELEASE_HTTP_TIMEOUT", "30.0"))
_FTP_RELEASE_MAX_BYTES = 2 * 1024 * 1024
_FTP_RELEASE_MAX_ENTRIES = 1000
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


class NcbiFtpRelease(TypedDict):
    """Normalized release facts from one NCBI metadata-index row."""

    db_name: str
    last_updated: str
    number_of_volumes: int
    bytes_total: int
    number_of_sequences: int


_FTP_RELEASE_CACHE_LOCK = threading.Lock()
_FTP_RELEASE_CACHE: tuple[float, dict[str, NcbiFtpRelease]] | None = None


def _reset_ftp_release_cache() -> None:
    """Clear the process-local index cache for tests."""
    global _FTP_RELEASE_CACHE
    with _FTP_RELEASE_CACHE_LOCK:
        _FTP_RELEASE_CACHE = None


def _copy_releases(
    releases: dict[str, NcbiFtpRelease],
) -> dict[str, NcbiFtpRelease]:
    return {
        name: NcbiFtpRelease(
            db_name=release["db_name"],
            last_updated=release["last_updated"],
            number_of_volumes=release["number_of_volumes"],
            bytes_total=release["bytes_total"],
            number_of_sequences=release["number_of_sequences"],
        )
        for name, release in releases.items()
    }


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _marker_date(value: str | None) -> date | None:
    match = _DATE_RE.search(value or "")
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def ftp_release_is_newer(
    release_last_updated: str | None,
    *available_source_markers: str | None,
) -> bool:
    """Return whether an FTP release is newer than every available source.

    Source markers may be cloud snapshot names (for example
    ``2026-07-21-01-05-02``) or ISO timestamps. If legacy metadata carries no
    parseable source date, a valid FTP release is considered newer so the UI
    can disclose the advisory update instead of silently hiding it.
    """
    release_date = _marker_date(release_last_updated)
    if release_date is None:
        return False
    source_dates = [
        parsed
        for marker in available_source_markers
        if (parsed := _marker_date(marker)) is not None
    ]
    return not source_dates or release_date > max(source_dates)


def _parse_release_index(raw: bytes) -> dict[str, NcbiFtpRelease]:
    if len(raw) > _FTP_RELEASE_MAX_BYTES:
        raise NcbiUnavailable("NCBI FTP release index exceeded the response cap")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NcbiUnavailable("NCBI FTP release index was not valid JSON") from exc
    if not isinstance(payload, list):
        raise NcbiUnavailable("NCBI FTP release index had an unexpected shape")
    if len(payload) > _FTP_RELEASE_MAX_ENTRIES:
        raise NcbiUnavailable("NCBI FTP release index exceeded the entry cap")

    releases: dict[str, NcbiFtpRelease] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        db_name = str(item.get("dbname") or "").strip()
        last_updated = str(item.get("last-updated") or "").strip()
        if not _DB_NAME_RE.fullmatch(db_name) or _marker_date(last_updated) is None:
            continue
        releases[db_name] = NcbiFtpRelease(
            db_name=db_name,
            last_updated=last_updated,
            number_of_volumes=_nonnegative_int(item.get("number-of-volumes")),
            bytes_total=_nonnegative_int(item.get("bytes-total")),
            number_of_sequences=_nonnegative_int(item.get("number-of-sequences")),
        )
    if not releases:
        raise NcbiUnavailable("NCBI FTP release index contained no usable entries")
    return releases


def latest_ftp_releases(*, ttl_seconds: float | None = None) -> dict[str, NcbiFtpRelease]:
    """Return cached current NCBI FTP release facts keyed by database name."""
    global _FTP_RELEASE_CACHE

    now = time.monotonic()
    with _FTP_RELEASE_CACHE_LOCK:
        cached = _FTP_RELEASE_CACHE
        if cached is not None and cached[0] > now:
            return _copy_releases(cached[1])

    from api.services.httpx_pool import get_pooled_client

    client = get_pooled_client(
        "ncbi-ftp-releases",
        timeout=_FTP_RELEASE_HTTP_TIMEOUT_SECONDS,
    )
    try:
        response = client.get(_FTP_RELEASE_INDEX_URL)
    except httpx.HTTPError as exc:
        raise NcbiUnavailable(f"NCBI FTP release index: {type(exc).__name__}") from exc
    if response.status_code == 403:
        raise NcbiAccessDenied("NCBI FTP release index returned 403")
    if response.status_code >= 400:
        raise NcbiUnavailable(f"NCBI FTP release index HTTP {response.status_code}")

    releases = _parse_release_index(response.content)
    ttl = _FTP_RELEASE_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    with _FTP_RELEASE_CACHE_LOCK:
        _FTP_RELEASE_CACHE = (time.monotonic() + max(ttl, 0.0), releases)
    return _copy_releases(releases)


__all__ = ["NcbiFtpRelease", "ftp_release_is_newer", "latest_ftp_releases"]
