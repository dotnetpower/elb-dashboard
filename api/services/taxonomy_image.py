"""Best-effort thumbnail lookup for an organism scientific name.

Responsibility: Best-effort thumbnail lookup for an organism scientific name
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `TaxonomyImageUnavailable`, `fetch_taxonomy_image`,
`clear_taxonomy_image_cache`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

LOGGER = logging.getLogger(__name__)

WIKIPEDIA_BASE_URL = "https://en.wikipedia.org/api/rest_v1"
DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_NAME_CHARS = 120
MAX_BODY_BYTES = 64 * 1024
MAX_CACHE_ENTRIES = 1024

# Allow letters (incl. accents), digits, space, hyphen, dot, parens and the
# multiplication sign used in hybrid names ("×"). Everything else (slashes,
# query separators, control chars) is rejected so the value can be safely
# slugged into the Wikipedia URL path.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9\u00C0-\u024F .\-()×]+$")

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class TaxonomyImageUnavailable(RuntimeError):
    """Raised only for programming errors (invalid name). Network failures
    are swallowed and surfaced as ``image_url=None`` so the UI degrades
    gracefully."""


def fetch_taxonomy_image(name: str) -> dict[str, Any]:
    """Return a thumbnail descriptor for an organism's scientific name.

    Shape::

        {
          "name": "Homo sapiens",
          "image_url": "https://upload.wikimedia.org/.../330px-...jpg" | None,
          "page_url": "https://en.wikipedia.org/wiki/Human" | None,
          "source": "wikipedia",
          "cached": bool,
        }
    """
    normalised = _normalise_name(name)
    cached = _cache_get(normalised)
    if cached is not None:
        return {**cached, "cached": True}

    payload = _empty_payload(normalised)
    body = _fetch_wikipedia_summary(normalised)
    if body is not None:
        payload.update(_extract_from_summary(body))

    _cache_put(normalised, payload)
    return payload


def clear_taxonomy_image_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _normalise_name(name: str) -> str:
    value = " ".join(str(name or "").strip().split())
    if not value:
        raise TaxonomyImageUnavailable("scientific name is required")
    if len(value) > MAX_NAME_CHARS:
        raise TaxonomyImageUnavailable(
            f"scientific name must be {MAX_NAME_CHARS} characters or fewer"
        )
    if not _NAME_PATTERN.fullmatch(value):
        raise TaxonomyImageUnavailable("scientific name contains unsupported characters")
    return value


def _empty_payload(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "image_url": None,
        "page_url": None,
        "source": "wikipedia",
        "cached": False,
    }


def _fetch_wikipedia_summary(name: str) -> dict[str, Any] | None:
    """Fetch the wiki summary; return ``None`` on any non-OK condition."""
    slug = quote(name.replace(" ", "_"), safe="")
    endpoint = f"/page/summary/{slug}"
    from api.services.httpx_pool import get_pooled_client

    client = get_pooled_client(
        "taxonomy-wikipedia-summary",
        timeout=DEFAULT_TIMEOUT_SECONDS,
        base_url=WIKIPEDIA_BASE_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "elb-dashboard/1.0 (taxonomy image lookup)",
        },
    )
    try:
        with client.stream("GET", endpoint) as response:
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                LOGGER.info(
                    "taxonomy image upstream status=%s name=%s",
                    response.status_code,
                    name,
                )
                return None
            buffer = bytearray()
            for chunk in response.iter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_BODY_BYTES:
                    LOGGER.info("taxonomy image upstream exceeded size cap name=%s", name)
                    return None
            try:
                parsed = json.loads(buffer)
            except ValueError:
                return None
            if not isinstance(parsed, dict):
                return None
            return parsed
    except httpx.HTTPError as exc:
        LOGGER.info("taxonomy image network failure name=%s: %s", name, exc.__class__.__name__)
        return None


def _extract_from_summary(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    thumbnail = body.get("thumbnail")
    if isinstance(thumbnail, dict):
        src = thumbnail.get("source")
        if isinstance(src, str) and src.startswith("https://upload.wikimedia.org/"):
            out["image_url"] = src
    content_urls = body.get("content_urls")
    if isinstance(content_urls, dict):
        desktop = content_urls.get("desktop")
        if isinstance(desktop, dict):
            page = desktop.get("page")
            if isinstance(page, str) and page.startswith("https://en.wikipedia.org/"):
                out["page_url"] = page
    return out


def _cache_get(name: str) -> dict[str, Any] | None:
    key = name.lower()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            _CACHE.pop(key, None)
            return None
        return dict(payload)


def _cache_put(name: str, payload: dict[str, Any]) -> None:
    key = name.lower()
    with _CACHE_LOCK:
        if len(_CACHE) >= MAX_CACHE_ENTRIES:
            try:
                oldest = next(iter(_CACHE))
                _CACHE.pop(oldest, None)
            except StopIteration:
                pass
        _CACHE[key] = (
            time.monotonic() + DEFAULT_CACHE_TTL_SECONDS,
            dict(payload),
        )
