"""Shared Storage route helpers.

Responsibility: Shared Storage route helpers
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `_check`, `_resolve_latest_dir`, `_list_keys`
Risky contracts: Never issue browser SAS URLs; local public Storage access remains debug-only
and IP-allowlisted.
Validation: `uv run pytest -q api/tests/test_storage_data.py
api/tests/test_storage_public_access.py`.
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
    """
    import httpx

    now = time.monotonic()
    with _NCBI_CACHE_LOCK:
        cached = _LATEST_DIR_CACHE.get(_NCBI_S3_BASE)
        if cached and cached[0] > now:
            return cached[1]
    resp = httpx.get(f"{_NCBI_S3_BASE}/latest-dir", timeout=15.0)
    resp.raise_for_status()
    latest = resp.text.strip()
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
    """
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
    # Hard cap at 50 pages x 1000 objects = 50k keys to bound surprise.
    with httpx.Client(timeout=30.0) as client:
        for _page in range(50):
            list_url = f"{_NCBI_S3_BASE}?list-type=2&prefix={prefix}&max-keys=1000"
            if continuation:
                list_url += f"&continuation-token={continuation}"
            resp = client.get(list_url)
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
_NCBI_CACHE_LOCK = threading.Lock()


def reset_ncbi_catalogue_cache() -> None:
    """Test hook: drop both NCBI catalogue caches."""
    with _NCBI_CACHE_LOCK:
        _LATEST_DIR_CACHE.clear()
        _LIST_KEYS_CACHE.clear()
