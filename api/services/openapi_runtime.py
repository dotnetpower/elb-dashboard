"""Runtime endpoint cache for the ElasticBLAST OpenAPI service."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import redis

LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY = "openapi:runtime:base-url"


def _redis_url() -> str:
    return os.environ.get("OPS_REDIS_URL", "redis://127.0.0.1:6379/2")


def _normalise_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def save_openapi_base_url(
    base_url: str,
    *,
    metadata: dict[str, Any] | None = None,
    client: Any | None = None,
) -> bool:
    """Persist the currently reachable OpenAPI base URL in ops Redis."""
    url = _normalise_base_url(base_url)
    if not url:
        return False
    payload = {
        "base_url": url,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    redis_client = client or redis.Redis.from_url(_redis_url(), socket_timeout=1.5)
    try:
        redis_client.set(_RUNTIME_KEY, json.dumps(payload, separators=(",", ":")))
        return True
    except Exception as exc:
        LOGGER.warning("openapi runtime endpoint cache write failed: %s", exc)
        return False


def get_openapi_base_url(*, client: Any | None = None) -> str:
    """Return the cached OpenAPI base URL, or an empty string if unavailable."""
    redis_client = client or redis.Redis.from_url(_redis_url(), socket_timeout=1.5)
    try:
        raw = redis_client.get(_RUNTIME_KEY)
    except Exception as exc:
        LOGGER.debug("openapi runtime endpoint cache read failed: %s", exc)
        return ""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return _normalise_base_url(str(raw))
    if not isinstance(payload, dict):
        return ""
    return _normalise_base_url(str(payload.get("base_url") or ""))
