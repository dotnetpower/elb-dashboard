"""Shared Storage route helpers.

Responsibility: Shared Storage route helpers
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_check`, `_resolve_latest_dir`, `_list_keys`, `shared_taxonomy_keys`
Risky contracts: Never issue browser SAS URLs; local public Storage access remains debug-only
and IP-allowlisted. `shared_taxonomy_keys` must keep negative results uncached so a transient
NCBI 5xx cannot pin the next hour of prepare-db runs into a taxonomy-less state.
Validation: `uv run pytest -q api/tests/test_storage_data.py
api/tests/test_storage_public_access.py api/tests/test_storage_shared_taxonomy.py`.
"""

from __future__ import annotations

import os
import re
import threading
import time
from xml.etree import ElementTree

from fastapi import HTTPException

from api.services.sanitise import sanitise

_NCBI_S3_BASE = "https://ncbi-blast-databases.s3.amazonaws.com"
_S3_LIST_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


class NcbiAccessDenied(RuntimeError):
    """S3 returned 403 — distinguishes throttling/IAM from "DB not found"."""


class NcbiUnavailable(RuntimeError):
    """S3 returned 5xx, network failed, or response was malformed."""

# Validation patterns — kept narrow on purpose. NCBI database names are
# `[A-Za-z0-9_]+` (e.g. `16S_ribosomal_RNA`, `core_nt`), storage account
# names are `[a-z0-9]{3,24}`, resource groups follow the ARM rules below.
_RE_DB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RE_STORAGE_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")


def _check(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or not pattern.match(value):
        raise HTTPException(400, f"invalid {label}: '{sanitise(str(value)[:40])}'")


def _resolve_latest_dir() -> str:
    """Return the latest snapshot directory name from NCBI.

    NCBI updates ``latest-dir`` once per snapshot day. We cache for 1 h
    (env-overridable via ``NCBI_LATEST_DIR_CACHE_TTL``) so the prepare-db
    + warmup paths don't make a fresh HTTP round-trip every call.
    Failures are NOT cached — a transient throttle or DNS hiccup should not
    poison the next 60 minutes of requests. Repeated failures trip the
    circuit breaker for ``_NCBI_BREAKER_COOLDOWN`` seconds.
    """
    import httpx

    now = time.monotonic()
    with _NCBI_CACHE_LOCK:
        cached = _LATEST_DIR_CACHE.get(_NCBI_S3_BASE)
        if cached and cached[0] > now:
            return cached[1]
    _breaker_check()
    try:
        resp = httpx.get(f"{_NCBI_S3_BASE}/latest-dir", timeout=15.0)
    except httpx.HTTPError as exc:
        _breaker_record_failure()
        raise NcbiUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code == 403:
        _breaker_record_failure()
        raise NcbiAccessDenied(f"NCBI bucket returned 403 for latest-dir: {resp.text[:200]}")
    if resp.status_code >= 500 or resp.status_code == 404:
        _breaker_record_failure()
        raise NcbiUnavailable(f"NCBI latest-dir HTTP {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    latest = resp.text.strip()
    if not latest:
        _breaker_record_failure()
        raise NcbiUnavailable("NCBI latest-dir returned empty body")
    _breaker_record_success()
    expires_at = time.monotonic() + _LATEST_DIR_CACHE_TTL_SECONDS
    with _NCBI_CACHE_LOCK:
        _LATEST_DIR_CACHE[_NCBI_S3_BASE] = (expires_at, latest)
    return latest


def _list_keys(latest_dir: str, db_name: str) -> list[str]:
    """List the S3 keys for ``{latest_dir}/{db_name}*``.

    NCBI publishes BLAST DBs as multiple sharded files plus a few small
    metadata files; for large DBs (``core_nt``, ``nr``) this is hundreds
    of objects spread across paginated XML responses. Result is cached for
    ``NCBI_LIST_KEYS_CACHE_TTL`` (default 1 h) keyed by
    ``(latest_dir, db_name)`` because the contents of a given snapshot
    directory never change after NCBI publishes it.

    Hardening:
      * 403 responses raise ``NcbiAccessDenied`` so the caller can return a
        clearer 502 ("NCBI throttled or restricted") instead of looking
        like a 404 ("DB not in snapshot").
      * Empty results are NOT cached. NCBI occasionally lists a snapshot
        directory before all DB objects have been published; caching an
        empty list would lock the next 60 minutes into a false "DB has no
        files" answer until process restart.
      * 5xx / network errors raise ``NcbiUnavailable`` (uncached).
    """
    from urllib.parse import quote

    import httpx

    cache_key = (latest_dir, db_name)
    now = time.monotonic()
    with _NCBI_CACHE_LOCK:
        cached = _LIST_KEYS_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return list(cached[1])

    prefix = f"{latest_dir}/{db_name}"
    keys: list[str] = []
    continuation = ""
    _breaker_check()
    # Hard cap at 50 pages x 1000 objects = 50k keys to bound surprise.
    try:
        from api.services.httpx_pool import get_pooled_client

        client = get_pooled_client("ncbi-list", timeout=30.0)
        for _page in range(50):
            list_url = f"{_NCBI_S3_BASE}?list-type=2&prefix={prefix}&max-keys=1000"
            if continuation:
                # S3 continuation tokens are opaque base64-ish blobs that
                # routinely contain '+' and '/' — both URL-significant. The
                # token MUST be percent-encoded or S3 returns HTTP 400
                # "The continuation token provided is incorrect" on page 2
                # of every multi-page DB (e.g. `nt`, `nr`). Quote with
                # safe='' so '/' is also encoded.
                list_url += f"&continuation-token={quote(continuation, safe='')}"
            resp = client.get(list_url)
            if resp.status_code == 403:
                _breaker_record_failure()
                raise NcbiAccessDenied(
                    f"NCBI bucket returned 403 listing {prefix!r}: {resp.text[:200]}"
                )
            if resp.status_code >= 500:
                _breaker_record_failure()
                raise NcbiUnavailable(
                    f"NCBI bucket HTTP {resp.status_code} listing {prefix!r}"
                )
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)  # noqa: S314 — NCBI public bucket, schema fixed
            for el in root.findall(".//s3:Contents/s3:Key", _S3_LIST_NS):
                if el.text and not el.text.endswith("/"):
                    keys.append(el.text)
            is_truncated = root.findtext("s3:IsTruncated", "false", _S3_LIST_NS)
            if is_truncated == "true":
                tok = root.find("s3:NextContinuationToken", _S3_LIST_NS)
                continuation = tok.text if tok is not None and tok.text else ""
            else:
                break
    except httpx.HTTPError as exc:
        _breaker_record_failure()
        raise NcbiUnavailable(f"{type(exc).__name__}: {exc}") from exc
    _breaker_record_success()
    # Only cache non-empty results — see docstring.
    if keys:
        expires_at = time.monotonic() + _LIST_KEYS_CACHE_TTL_SECONDS
        with _NCBI_CACHE_LOCK:
            _LIST_KEYS_CACHE[cache_key] = (expires_at, list(keys))
            if len(_LIST_KEYS_CACHE) > 64:
                oldest = min(_LIST_KEYS_CACHE.items(), key=lambda kv: kv[1][0])[0]
                _LIST_KEYS_CACHE.pop(oldest, None)
    return keys


_LATEST_DIR_CACHE_TTL_SECONDS = float(
    os.environ.get("NCBI_LATEST_DIR_CACHE_TTL", "3600.0")
)
_LIST_KEYS_CACHE_TTL_SECONDS = float(
    os.environ.get("NCBI_LIST_KEYS_CACHE_TTL", "3600.0")
)
_LATEST_DIR_CACHE: dict[str, tuple[float, str]] = {}
_LIST_KEYS_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_SHARED_TAXONOMY_KEYS_CACHE: dict[str, tuple[float, list[str]]] = {}
_NCBI_CACHE_LOCK = threading.Lock()

# Snapshot-root files that ``blastn``/``blastp`` need for taxonomy-aware
# output (``-outfmt '... staxid ssciname scomname sblastname'``) and for the
# v4 taxonomy lookup path. Each NCBI snapshot publishes these at
# ``{latest_dir}/<name>`` (i.e. they are NOT prefixed by any database name).
# ``_list_keys`` uses ``{latest_dir}/{db_name}`` so it never picks these up —
# the prepare-db pipeline therefore needs to stage them explicitly. The
# basename is preserved on the destination so the warmup script's existing
# ``--include-pattern "...;taxdb.btd;taxdb.bti;taxonomy4blast.sqlite3;..."``
# finds them inside each per-DB folder without any pattern change.
SHARED_TAXONOMY_FILES: tuple[str, ...] = (
    "taxdb.btd",
    "taxdb.bti",
    "taxonomy4blast.sqlite3",
)

# Circuit breaker — after `_NCBI_BREAKER_THRESHOLD` consecutive 403 / 5xx
# events from NCBI we open the circuit for `_NCBI_BREAKER_COOLDOWN` seconds.
# While open, every NCBI helper raises immediately without hitting the
# network, so users see fast actionable errors instead of N seconds of timeout
# per call and NCBI sees zero traffic from us during the cooldown.
_NCBI_BREAKER_THRESHOLD = int(os.environ.get("NCBI_BREAKER_THRESHOLD", "5"))
_NCBI_BREAKER_COOLDOWN = float(os.environ.get("NCBI_BREAKER_COOLDOWN", "120.0"))
_NCBI_BREAKER_STATE: dict[str, float | int] = {"failures": 0, "opened_at": 0.0}


def _breaker_open() -> bool:
    """Return True if the circuit is currently open (calls should be refused)."""
    opened_at = float(_NCBI_BREAKER_STATE.get("opened_at") or 0.0)
    if opened_at <= 0:
        return False
    if time.monotonic() - opened_at >= _NCBI_BREAKER_COOLDOWN:
        # Cooldown elapsed — close + reset for next attempt.
        _NCBI_BREAKER_STATE["opened_at"] = 0.0
        _NCBI_BREAKER_STATE["failures"] = 0
        return False
    return True


def _breaker_record_failure() -> None:
    failures = int(_NCBI_BREAKER_STATE.get("failures") or 0) + 1
    _NCBI_BREAKER_STATE["failures"] = failures
    if failures >= _NCBI_BREAKER_THRESHOLD:
        _NCBI_BREAKER_STATE["opened_at"] = time.monotonic()


def _breaker_record_success() -> None:
    _NCBI_BREAKER_STATE["failures"] = 0
    _NCBI_BREAKER_STATE["opened_at"] = 0.0


def _breaker_check() -> None:
    """Raise ``NcbiUnavailable`` if the circuit is open."""
    if _breaker_open():
        raise NcbiUnavailable(
            f"NCBI circuit open ({_NCBI_BREAKER_THRESHOLD}+ consecutive failures); "
            f"will retry after {_NCBI_BREAKER_COOLDOWN:.0f}s cooldown"
        )


def reset_ncbi_catalogue_cache() -> None:
    """Test hook: drop both NCBI catalogue caches and the breaker state."""
    with _NCBI_CACHE_LOCK:
        _LATEST_DIR_CACHE.clear()
        _LIST_KEYS_CACHE.clear()
        _SHARED_TAXONOMY_KEYS_CACHE.clear()
        _NCBI_BREAKER_STATE["failures"] = 0
        _NCBI_BREAKER_STATE["opened_at"] = 0.0


def shared_taxonomy_keys(latest_dir: str) -> list[str]:
    """Return the subset of ``SHARED_TAXONOMY_FILES`` that exist in this
    snapshot, as fully-qualified ``{latest_dir}/<name>`` S3 keys.

    Done by issuing one HEAD per file (3 small requests) and keeping only
    the 200 OK ones. The result is cached per ``latest_dir`` for
    ``NCBI_LIST_KEYS_CACHE_TTL`` (same TTL as ``_list_keys``) because a
    given snapshot directory is immutable once published.

    Failure semantics mirror ``_list_keys``:
      * 403 → ``NcbiAccessDenied`` (caller surfaces as 502 "throttled")
      * 5xx / network → ``NcbiUnavailable``
      * 404 on an individual file is NOT an error — that file is simply
        absent from the snapshot and is skipped (NCBI occasionally drops
        ``taxonomy4blast.sqlite3`` while regenerating). The remaining
        files are still returned.
      * Empty results are NOT cached so a transient outage cannot poison
        the next hour of prepare-db calls into "no taxonomy files".

    The circuit breaker is honoured so a sustained NCBI outage skips the
    HEADs entirely rather than burning the cooldown on three more probes.
    """
    import httpx

    now = time.monotonic()
    with _NCBI_CACHE_LOCK:
        cached = _SHARED_TAXONOMY_KEYS_CACHE.get(latest_dir)
        if cached and cached[0] > now:
            return list(cached[1])

    _breaker_check()
    found: list[str] = []
    try:
        from api.services.httpx_pool import get_pooled_client

        client = get_pooled_client("ncbi-tax", timeout=15.0)
        for name in SHARED_TAXONOMY_FILES:
            key = f"{latest_dir}/{name}"
            url = f"{_NCBI_S3_BASE}/{key}"
            resp = client.head(url)
            if resp.status_code == 200:
                found.append(key)
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code == 403:
                _breaker_record_failure()
                raise NcbiAccessDenied(
                    f"NCBI bucket returned 403 on HEAD {key!r}"
                )
            if resp.status_code >= 500:
                _breaker_record_failure()
                raise NcbiUnavailable(
                    f"NCBI bucket HTTP {resp.status_code} on HEAD {key!r}"
                )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        _breaker_record_failure()
        raise NcbiUnavailable(f"{type(exc).__name__}: {exc}") from exc
    _breaker_record_success()
    if found:
        expires_at = time.monotonic() + _LIST_KEYS_CACHE_TTL_SECONDS
        with _NCBI_CACHE_LOCK:
            _SHARED_TAXONOMY_KEYS_CACHE[latest_dir] = (expires_at, list(found))
    return found
