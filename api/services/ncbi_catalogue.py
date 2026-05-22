"""NCBI BLAST DB catalogue preview + per-DB update detection helpers.

Responsibility: Surface read-only NCBI snapshot facts for a given DB name (file
    count, total bytes, snapshot last-modified) so the SPA can render version
    info BEFORE the user clicks Download, and detect per-DB updates that the
    global ``latest-dir`` comparison alone cannot see (NCBI rotates
    ``latest-dir`` even when the requested DB itself did not change).
Edit boundaries: Pure NCBI S3 catalogue helpers — no Azure SDK, no FastAPI
    objects, no logging beyond debug. All HTTP through httpx; failures map to
    the two exception types defined in ``api.routes.storage.common``.
Key entry points: ``preview_database``, ``database_update_signature``,
    ``RE_DB_NAME``.
Risky contracts: Public ``preview_database`` shape is consumed by
    ``GET /api/blast/databases/{db}/preview`` (web/src/api/blast.ts
    ``previewDatabase``); keep keys backwards-compatible.
Validation: ``uv run pytest -q api/tests/test_ncbi_catalogue.py``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

import httpx

from api.routes.storage.common import (
    _NCBI_S3_BASE,
    NcbiAccessDenied,
    NcbiUnavailable,
    _list_keys,
    _resolve_latest_dir,
)

LOGGER = logging.getLogger(__name__)

# Mirrors api.routes.storage.common._RE_DB_NAME but exported so the route layer
# AND the SPA-bundled validator share one regex (sync via codegen comment).
RE_DB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# A small allowlist of file extensions we treat as "the DB itself" for sizing.
# Catalogue files (.tar.gz manifests, README) are kept in file_count but called
# out separately because their absence does not block BLAST.
#
# Keep this list in lock-step with `_DB_EXTS` in
# api/services/storage_data.py:list_databases — both must agree on what
# counts as a BLAST DB file or the dashboard's "downloaded" detection drifts
# from the preview's "volume_count".
_BLAST_VOLUME_SUFFIXES = (
    ".nhr",
    ".nin",
    ".nsq",
    ".nhd",
    ".nhi",
    ".nog",
    ".nal",
    ".njs",
    ".ndb",
    ".nos",
    ".not",
    ".ntf",
    ".nto",
    ".nnd",
    ".nni",
    ".nxm",
    ".phr",
    ".pin",
    ".psq",
    ".phd",
    ".phi",
    ".pog",
    ".pal",
    ".pjs",
    ".pdb",
    ".pos",
    ".pot",
    ".ptf",
    ".pto",
    ".pnd",
    ".pni",
    ".pxm",
)
# How many representative `.tar.gz.md5` files we HEAD when building the
# composite signature. For a sharded DB with 50 volumes, NCBI rotates one
# .md5 per shard; sampling only the first would miss updates that only
# touched a later shard. ``8`` keeps us well under NCBI's per-IP HEAD budget
# while detecting almost every real change.
_SIGNATURE_SAMPLE_COUNT = int(os.environ.get("NCBI_SIGNATURE_SAMPLE_COUNT", "8"))
# Explicit Client-wide timeout for httpx.Client() so cold CloudFront paths
# (slow TLS handshake from a fresh sidecar) do not 5s-default themselves
# into a NcbiUnavailable.
_PREVIEW_CLIENT_TIMEOUT = float(os.environ.get("NCBI_PREVIEW_HTTP_TIMEOUT", "30.0"))

_PREVIEW_CACHE_TTL_SECONDS = float(os.environ.get("NCBI_PREVIEW_CACHE_TTL", "1800.0"))
_PREVIEW_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_PREVIEW_CACHE_LOCK = threading.Lock()


def _reset_preview_cache() -> None:
    """Test hook."""
    with _PREVIEW_CACHE_LOCK:
        _PREVIEW_CACHE.clear()


def _head_key(client: httpx.Client, key: str) -> dict[str, Any]:
    """HEAD a single S3 key and return ``{etag, last_modified, size}``.

    Returns an empty dict on 404/403 so the preview surface can still render
    the file count without imploding on permission glitches. Never raises.
    """
    try:
        resp = client.head(f"{_NCBI_S3_BASE}/{key}", timeout=15.0)
    except httpx.HTTPError as exc:
        LOGGER.debug("HEAD %s failed: %s", key, type(exc).__name__)
        return {}
    if resp.status_code >= 400:
        return {}
    return {
        "etag": (resp.headers.get("ETag") or "").strip('"'),
        "last_modified": resp.headers.get("Last-Modified", ""),
        "size": int(resp.headers.get("Content-Length", "0") or 0),
    }


def _pick_signature_keys(db_name: str, keys: list[str]) -> list[str]:
    """Choose deterministic NCBI keys whose ETags track the DB generation.

    For a single-volume DB this returns one key (the lone ``.tar.gz.md5``).
    For sharded DBs (``core_nt``, ``nr``, ``nt``) we sample
    ``_SIGNATURE_SAMPLE_COUNT`` evenly-spaced ``.tar.gz.md5`` files so that
    an update touching only a later shard still flips the composite
    signature — without making N HEADs for N volumes.
    """
    md5s = sorted(k for k in keys if k.endswith(".tar.gz.md5"))
    if md5s:
        n = max(1, min(_SIGNATURE_SAMPLE_COUNT, len(md5s)))
        if len(md5s) <= n:
            return md5s
        # Evenly-spaced sample including first and last.
        step = (len(md5s) - 1) / (n - 1)
        sampled = [md5s[int(round(i * step))] for i in range(n)]
        # Dedup preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for k in sampled:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out
    targz = sorted(k for k in keys if k.endswith(".tar.gz"))
    if targz:
        return targz[:1]
    for suffix in (".nhr", ".phr", ".nal", ".pal"):
        candidates = sorted(k for k in keys if k.endswith(suffix))
        if candidates:
            return candidates[:1]
    return [keys[0]] if keys else []


def _composite_etag(keys_with_etag: list[tuple[str, str]]) -> str:
    """Hash a list of (key, etag) pairs into a single deterministic marker.

    The hash input includes the key name + etag so a re-order of keys or a
    missing key (which would yield empty etag) both flip the composite. Uses
    SHA-256 truncated to 16 hex chars to stay short in metadata.json.
    """
    import hashlib

    h = hashlib.sha256()
    for key, etag in sorted(keys_with_etag):
        h.update(key.encode("utf-8"))
        h.update(b"\x00")
        h.update((etag or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# Back-compat shim — older imports may still grab the singular variant.
def _pick_signature_key(db_name: str, keys: list[str]) -> str | None:
    sampled = _pick_signature_keys(db_name, keys)
    return sampled[0] if sampled else None


def preview_database(db_name: str) -> dict[str, Any]:
    """Dry-run summary of a NCBI BLAST DB the user might click Download on.

    Returns:
        ``{
            db_name, snapshot, available, file_count, volume_count,
            total_bytes, last_modified, signature_key, signature_etag,
            files_sample, source
        }``

    Behaviour:
      * Caches per ``(snapshot, db_name)`` for ``NCBI_PREVIEW_CACHE_TTL``
        (default 30 min). An NCBI snapshot directory is immutable once
        published, so re-fetching ETags every render is wasteful.
      * ``available=False`` when the DB has zero S3 objects in the current
        snapshot. The route layer translates this into the "this DB is only on
        NCBI FTP, not the S3 mirror" hint surfaced in the SPA.
      * Honest with failures: ``NcbiAccessDenied`` and ``NcbiUnavailable``
        bubble up so the route can map them to the right HTTP status / SPA
        toast wording. We do not invent fake data.
    """
    if not RE_DB_NAME.match(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")

    snapshot = _resolve_latest_dir()
    cache_key = (snapshot, db_name)
    now = time.monotonic()
    with _PREVIEW_CACHE_LOCK:
        cached = _PREVIEW_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    keys = _list_keys(snapshot, db_name)
    if not keys:
        # We intentionally do NOT cache the negative result here. _list_keys
        # already refuses to cache empties (because NCBI sometimes lists a
        # snapshot directory mid-publish), so we mirror that policy.
        return {
            "db_name": db_name,
            "snapshot": snapshot,
            "available": False,
            "file_count": 0,
            "volume_count": 0,
            "total_bytes": 0,
            "last_modified": None,
            "signature_key": None,
            "signature_etag": None,
            "files_sample": [],
            "source": "ncbi-s3",
            "message": (
                "This database is not present in the current NCBI S3 snapshot. "
                "It may be FTP-only or published in a different latest-dir. "
                "Wait a few minutes for NCBI to finish publishing the snapshot "
                "or retry once the snapshot id changes."
            ),
        }

    volume_keys = [k for k in keys if k.endswith(_BLAST_VOLUME_SUFFIXES)]
    signature_keys = _pick_signature_keys(db_name, keys)
    head_for_size_probe: dict[str, Any] = {}
    signature_heads: list[tuple[str, dict[str, Any]]] = []

    # HEAD up to (N + 1) objects:
    #   * N signature keys (md5 / tar.gz) — drive composite generation ETag.
    #     The HEADs run inside one Client so the TLS handshake amortises.
    #   * 1 representative volume — drives size estimate when the snapshot
    #     list does not carry sizes (it doesn't; only HEAD does).
    with httpx.Client(timeout=_PREVIEW_CLIENT_TIMEOUT) as client:
        for sig_key in signature_keys:
            signature_heads.append((sig_key, _head_key(client, sig_key)))
        size_probe_key = volume_keys[0] if volume_keys else keys[0]
        if size_probe_key and size_probe_key not in signature_keys:
            head_for_size_probe = _head_key(client, size_probe_key)

    # Composite signature: hash all sampled (key, etag) pairs into one short
    # marker. Detects updates on later shards that a single-key signature
    # would miss (Critique §5).
    composite = _composite_etag(
        [(k, str(h.get("etag") or "")) for k, h in signature_heads]
    )
    primary_signature_key = signature_keys[0] if signature_keys else None
    primary_signature_etag = signature_heads[0][1].get("etag") if signature_heads else None

    # Total-bytes estimate: probe size * volume_count. Conservative; we
    # surface it as ``total_bytes_estimate`` to keep ``total_bytes`` semantics
    # unambiguous (it's a known sum only after the download lands).
    probe_size = int(
        head_for_size_probe.get("size", 0)
        or (signature_heads[0][1].get("size", 0) if signature_heads else 0)
    )
    total_bytes_estimate = probe_size * max(len(volume_keys), 1)
    last_modified = (
        signature_heads[0][1].get("last_modified") if signature_heads else None
    ) or head_for_size_probe.get("last_modified")

    summary = {
        "db_name": db_name,
        "snapshot": snapshot,
        "available": True,
        "file_count": len(keys),
        "volume_count": len(volume_keys),
        "total_bytes_estimate": total_bytes_estimate,
        "last_modified": last_modified,
        "signature_key": primary_signature_key,
        "signature_etag": primary_signature_etag or None,
        # New: hashed composite of all sampled signature keys. SPA + check-updates
        # use this when available; falls back to signature_etag for legacy.
        "composite_signature": composite,
        "signature_sample_count": len(signature_heads),
        "files_sample": sorted(keys)[:8],
        "source": "ncbi-s3",
    }
    expires_at = time.monotonic() + _PREVIEW_CACHE_TTL_SECONDS
    with _PREVIEW_CACHE_LOCK:
        _PREVIEW_CACHE[cache_key] = (expires_at, dict(summary))
        if len(_PREVIEW_CACHE) > 128:
            oldest = min(_PREVIEW_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _PREVIEW_CACHE.pop(oldest, None)
    return summary


def database_update_signature(db_name: str) -> dict[str, Any]:
    """Lightweight per-DB generation marker for update detection.

    Returns ``{snapshot, signature_key, signature_etag, composite_signature,
    last_modified, available}``. The SPA / check-updates path compares
    ``composite_signature`` first (multi-shard safe) and falls back to
    ``signature_etag`` for legacy DBs whose metadata was written before
    composite signatures were added.

    Safe to call from the SPA poll loop — re-uses ``preview_database`` cache.
    """
    preview = preview_database(db_name)
    return {
        "snapshot": preview.get("snapshot"),
        "available": bool(preview.get("available")),
        "signature_key": preview.get("signature_key"),
        "signature_etag": preview.get("signature_etag"),
        "composite_signature": preview.get("composite_signature"),
        "signature_sample_count": preview.get("signature_sample_count"),
        "last_modified": preview.get("last_modified"),
    }


__all__ = [
    "RE_DB_NAME",
    "NcbiAccessDenied",
    "NcbiUnavailable",
    "_reset_preview_cache",
    "database_update_signature",
    "preview_database",
]
