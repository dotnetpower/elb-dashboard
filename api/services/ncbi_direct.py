"""Immutable NCBI Direct transfer manifests for BLAST databases.

Responsibility: Fetch the official NCBI HTTPS metadata and archive checksums,
    validate their fixed-host URLs, and build a deterministic transfer manifest.
Edit boundaries: Read-only NCBI discovery only; Kubernetes dispatch, archive
    extraction, Azure uploads, metadata promotion, and HTTP responses stay in
    their owning layers.
Key entry points: `NcbiDirectManifest`, `build_direct_manifest`.
Risky contracts: Only HTTPS URLs under `ftp.ncbi.nlm.nih.gov/blast/db/` are
    accepted; every archive requires an official MD5 and positive byte size;
    failures are never cached.
Validation: `uv run pytest -q api/tests/test_ncbi_direct.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from api.routes.storage.common import NcbiAccessDenied, NcbiUnavailable
from api.services.db.generations import generation_id, release_fingerprint

_METADATA_URL = "https://ftp.ncbi.nlm.nih.gov/blast/db/v5/blastdb-metadata-1-1.json"
_ALLOWED_HOST = "ftp.ncbi.nlm.nih.gov"
_ALLOWED_PATH_PREFIX = "/blast/db/"
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVES = 1024
_MD5_RE = re.compile(r"^([0-9a-fA-F]{32})(?:\s+[*]?([^\s]+))?\s*$")
_DB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TIMEOUT = float(os.environ.get("NCBI_DIRECT_HTTP_TIMEOUT", "60"))
_CACHE_TTL = float(os.environ.get("NCBI_DIRECT_MANIFEST_TTL", "1800"))
_PROBE_WORKERS = max(1, min(int(os.environ.get("NCBI_DIRECT_PROBE_WORKERS", "8")), 16))
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, NcbiDirectManifest]] = {}


@dataclass(frozen=True)
class NcbiDirectArchive:
    """One immutable archive input in a direct-transfer manifest."""

    url: str
    md5_url: str
    md5: str
    size: int
    member_prefix: str = ""


@dataclass(frozen=True)
class NcbiDirectManifest:
    """Pinned logical release and all archive transfer inputs."""

    db_name: str
    released_at: str
    release_fingerprint: str
    generation_id: str
    transfer_manifest_sha256: str
    number_of_letters: int
    number_of_sequences: int
    bytes_total: int
    bytes_total_compressed: int
    archives: tuple[NcbiDirectArchive, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_archive_url(url: str, db_name: str) -> str:
    parsed = urlparse(url)
    expected_prefix = f"{_ALLOWED_PATH_PREFIX}{db_name}"
    if (
        parsed.scheme not in {"https", "ftp"}
        or (parsed.hostname or "").lower() != _ALLOWED_HOST
        or not parsed.path.startswith(expected_prefix)
        or not parsed.path.endswith(".tar.gz")
        or parsed.query
        or parsed.fragment
    ):
        raise NcbiUnavailable("NCBI Direct metadata contained an unsafe archive URL")
    # NCBI's official JSON still spells file entries as ftp:// even though its
    # current downloader and cloud guidance prefer HTTPS. Normalize only the
    # exact allowlisted host/path; redirects remain disabled in our client.
    return parsed._replace(scheme="https").geturl()


def _parse_md5(raw: bytes, expected_name: str) -> str:
    if len(raw) > 1024:
        raise NcbiUnavailable("NCBI Direct checksum response exceeded the cap")
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise NcbiUnavailable("NCBI Direct checksum was not ASCII") from exc
    match = _MD5_RE.fullmatch(text)
    if match is None:
        raise NcbiUnavailable("NCBI Direct checksum had an invalid shape")
    named = match.group(2)
    if named and named.rsplit("/", 1)[-1] != expected_name:
        raise NcbiUnavailable("NCBI Direct checksum named a different archive")
    return match.group(1).lower()


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise NcbiUnavailable(f"NCBI Direct metadata had invalid {label}") from exc
    if parsed <= 0:
        raise NcbiUnavailable(f"NCBI Direct metadata had non-positive {label}")
    return parsed


def _nonnegative_int(value: Any, label: str) -> int:
    """Validate counts for non-searchable support archives such as taxdb."""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise NcbiUnavailable(f"NCBI Direct metadata had invalid {label}") from exc
    if parsed < 0:
        raise NcbiUnavailable(f"NCBI Direct metadata had negative {label}")
    return parsed


def _fetch_archive(client: httpx.Client, url: str, member_prefix: str) -> NcbiDirectArchive:
    md5_url = f"{url}.md5"
    try:
        checksum = client.get(md5_url)
        archive = client.head(url)
    except httpx.HTTPError as exc:
        raise NcbiUnavailable(f"NCBI Direct archive probe: {type(exc).__name__}") from exc
    if checksum.status_code == 403 or archive.status_code == 403:
        raise NcbiAccessDenied("NCBI Direct archive probe returned 403")
    if checksum.status_code >= 400 or archive.status_code >= 400:
        raise NcbiUnavailable(
            f"NCBI Direct archive probe HTTP {max(checksum.status_code, archive.status_code)}"
        )
    name = url.rsplit("/", 1)[-1]
    return NcbiDirectArchive(
        url=url,
        md5_url=md5_url,
        md5=_parse_md5(checksum.content, name),
        size=_positive_int(archive.headers.get("Content-Length"), "archive size"),
        member_prefix=member_prefix,
    )


def transfer_manifest_sha256(archives: tuple[NcbiDirectArchive, ...]) -> str:
    """Hash pinned transfer inputs, including each archive member namespace."""
    payload = [asdict(item) for item in archives]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _fetch_release(client: httpx.Client, db_name: str) -> dict[str, Any]:
    try:
        response = client.get(_METADATA_URL)
    except httpx.HTTPError as exc:
        raise NcbiUnavailable(f"NCBI Direct metadata: {type(exc).__name__}") from exc
    if response.status_code == 403:
        raise NcbiAccessDenied("NCBI Direct metadata returned 403")
    if response.status_code >= 400:
        raise NcbiUnavailable(f"NCBI Direct metadata HTTP {response.status_code}")
    if len(response.content) > _MAX_METADATA_BYTES:
        raise NcbiUnavailable("NCBI Direct metadata exceeded the response cap")
    try:
        payload = response.json()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NcbiUnavailable("NCBI Direct metadata was not valid JSON") from exc
    if not isinstance(payload, list) or len(payload) > 1000:
        raise NcbiUnavailable("NCBI Direct metadata had an unexpected shape")
    release = next(
        (item for item in payload if isinstance(item, dict) and item.get("dbname") == db_name),
        None,
    )
    if release is None:
        raise NcbiUnavailable(f"NCBI Direct metadata did not list {db_name}")
    return release


def build_direct_manifest(
    db_name: str,
    *,
    client: httpx.Client | None = None,
    ttl_seconds: float | None = None,
) -> NcbiDirectManifest:
    """Return a cached, immutable NCBI Direct manifest for one database."""
    if not _DB_RE.fullmatch(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(db_name)
        if cached is not None and cached[0] > now:
            return cached[1]

    owned_client = client is None
    active_client = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=False)
    try:
        release = _fetch_release(active_client, db_name)
        files = release.get("files")
        if not isinstance(files, list) or not files or len(files) > _MAX_ARCHIVES:
            raise NcbiUnavailable("NCBI Direct metadata had an invalid archive list")
        urls = tuple(_validate_archive_url(str(value), db_name) for value in files)
        if len(set(urls)) != len(urls):
            raise NcbiUnavailable("NCBI Direct metadata contained duplicate archives")
        archives_by_url: dict[str, NcbiDirectArchive] = {}
        with ThreadPoolExecutor(max_workers=min(_PROBE_WORKERS, len(urls))) as executor:
            futures = {
                executor.submit(_fetch_archive, active_client, url, db_name): url for url in urls
            }
            for future in as_completed(futures):
                archive = future.result()
                archives_by_url[archive.url] = archive
        archives = tuple(archives_by_url[url] for url in sorted(urls))
        released_at = str(release.get("last-updated") or "").strip()
        logical = {
            "last_updated": released_at,
            "number_of_letters": release.get("number-of-letters"),
            "number_of_sequences": release.get("number-of-sequences"),
            "number_of_volumes": release.get("number-of-volumes"),
            "bytes_total": release.get("bytes-total"),
        }
        fingerprint = release_fingerprint(db_name, logical)
        transfer_sha = transfer_manifest_sha256(archives)
        # ``taxdb`` is a taxonomy lookup bundle rather than a searchable BLAST
        # database. Its official metadata intentionally reports zero letters
        # and sequences; every searchable database must retain the stricter
        # positive-count contract used by planning and result metadata.
        count_validator = _nonnegative_int if db_name == "taxdb" else _positive_int
        manifest = NcbiDirectManifest(
            db_name=db_name,
            released_at=released_at,
            release_fingerprint=fingerprint,
            generation_id=generation_id("ncbi-direct", released_at, fingerprint),
            transfer_manifest_sha256=transfer_sha,
            number_of_letters=count_validator(release.get("number-of-letters"), "letter count"),
            number_of_sequences=count_validator(
                release.get("number-of-sequences"), "sequence count"
            ),
            bytes_total=_positive_int(release.get("bytes-total"), "database size"),
            bytes_total_compressed=_positive_int(
                release.get("bytes-total-compressed"), "compressed size"
            ),
            archives=archives,
        )
    finally:
        if owned_client:
            active_client.close()

    ttl = _CACHE_TTL if ttl_seconds is None else max(ttl_seconds, 0.0)
    with _CACHE_LOCK:
        _CACHE[db_name] = (time.monotonic() + ttl, manifest)
    return manifest


def reset_direct_manifest_cache() -> None:
    """Clear process-local manifests for tests and explicit refreshes."""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "NcbiDirectArchive",
    "NcbiDirectManifest",
    "build_direct_manifest",
    "reset_direct_manifest_cache",
    "transfer_manifest_sha256",
]
