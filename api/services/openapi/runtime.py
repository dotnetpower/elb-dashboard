"""Runtime endpoint cache for the ElasticBLAST OpenAPI service.

Responsibility: Runtime endpoint and API token cache for the ElasticBLAST OpenAPI service
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_redis_url`, `_normalise_base_url`, `save_openapi_base_url`,
`get_openapi_base_url`, `save_openapi_api_token`, `get_openapi_api_token`,
`save_openapi_public_base_url`, `get_openapi_public_base_url`,
`get_public_tls_base_url`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries. `get_public_tls_base_url` returns an empty string when neither
`OPENAPI_PUBLIC_BASE_URL` env nor the public-base-url cache is set, so legacy
call sites can short-circuit and keep using the IP-based path with zero
behaviour change.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from api.services.redis_clients import get_ops_redis_client

LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY = "openapi:runtime:base-url"
_TOKEN_KEY = "openapi:runtime:api-token"  # noqa: S105 - Redis key name, not a secret value.
_PUBLIC_BASE_URL_KEY = "openapi:runtime:public-base-url"


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
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        redis_client.set(_RUNTIME_KEY, json.dumps(payload, separators=(",", ":")))
        return True
    except Exception as exc:
        LOGGER.warning("openapi runtime endpoint cache write failed: %s", exc)
        return False


def get_openapi_base_url(*, client: Any | None = None) -> str:
    """Return the cached OpenAPI base URL, or an empty string if unavailable."""
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
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


def save_openapi_api_token(
    token: str,
    *,
    metadata: dict[str, Any] | None = None,
    client: Any | None = None,
) -> bool:
    """Persist the current OpenAPI API token in ops Redis."""
    value = token.strip()
    if not value:
        return False
    payload = {
        "token": value,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        redis_client.set(_TOKEN_KEY, json.dumps(payload, separators=(",", ":")))
        return True
    except Exception as exc:
        LOGGER.warning("openapi runtime token cache write failed: %s", type(exc).__name__)
        return False


def get_openapi_api_token(*, client: Any | None = None) -> str:
    """Return the cached OpenAPI API token, or an empty string if unavailable."""
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        raw = redis_client.get(_TOKEN_KEY)
    except Exception as exc:
        LOGGER.debug("openapi runtime token cache read failed: %s", type(exc).__name__)
        return ""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return str(raw).strip()
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("token") or "").strip()


# Public TLS endpoint hook. When `OPENAPI_PUBLIC_BASE_URL` is set (e.g.
# `https://openapi.example.com`) the dashboard's outbound calls to the
# sibling OpenAPI service prefer this URL over the in-cluster Service IP
# discovered via `k8s_get_service_ip`. Keeps the IP path 100% intact when
# the env is unset — domain rollout is opt-in at the env layer.
_PUBLIC_BASE_URL_ENV = "OPENAPI_PUBLIC_BASE_URL"


def save_openapi_public_base_url(
    base_url: str,
    *,
    metadata: dict[str, Any] | None = None,
    client: Any | None = None,
) -> bool:
    """Persist the public HTTPS endpoint (cloudapp.azure.com FQDN) in ops Redis.

    Written by the `setup_openapi_public_https` Celery task after the
    ingress-nginx + cert-manager + Ingress + Certificate pipeline turns
    Ready. Read by `get_public_tls_base_url` as a fallback to the
    `OPENAPI_PUBLIC_BASE_URL` env so the task can flip the dashboard to
    HTTPS without restarting the api / worker sidecar (which would mean
    a new Container App revision and a few seconds of cold start).
    """
    url = _normalise_base_url(base_url)
    if not url:
        return False
    payload = {
        "base_url": url,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        redis_client.set(_PUBLIC_BASE_URL_KEY, json.dumps(payload, separators=(",", ":")))
        return True
    except Exception as exc:
        LOGGER.warning("openapi public base url cache write failed: %s", exc)
        return False


def get_openapi_public_base_url(*, client: Any | None = None) -> dict[str, Any]:
    """Return the cached public HTTPS endpoint payload, or `{}` if unavailable.

    Full payload (with metadata + updated_at) is returned so the SPA's
    Public HTTPS panel can render the cert provenance / last-setup
    timestamp without a second round trip. Callers that only need the
    URL string should index `result.get("base_url", "")`.
    """
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        raw = redis_client.get(_PUBLIC_BASE_URL_KEY)
    except Exception as exc:
        LOGGER.debug("openapi public base url cache read failed: %s", exc)
        return {}
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        url = _normalise_base_url(str(raw))
        return {"base_url": url} if url else {}
    if not isinstance(payload, dict):
        return {}
    payload["base_url"] = _normalise_base_url(str(payload.get("base_url") or ""))
    if not payload["base_url"]:
        return {}
    return payload


def clear_openapi_public_base_url(*, client: Any | None = None) -> bool:
    """Drop the cached public HTTPS endpoint (used by DELETE / disable)."""
    redis_client = client or get_ops_redis_client(socket_timeout=1.5)
    try:
        redis_client.delete(_PUBLIC_BASE_URL_KEY)
        return True
    except Exception as exc:
        LOGGER.warning("openapi public base url cache delete failed: %s", exc)
        return False


def get_public_tls_base_url() -> str:
    """Return the operator-configured public TLS endpoint, or empty string.

    Empty string means "no domain configured yet — use the legacy IP
    path". Resolution order:
    1. ``OPENAPI_PUBLIC_BASE_URL`` env — operator can still hard-pin a
       custom domain (e.g. behind App Gateway) by setting this on the
       api / worker sidecars.
    2. Ops Redis cache populated by `setup_openapi_public_https` — lets
       the dashboard flip to HTTPS as soon as the Celery task finishes,
       no Container App revision required.
    """
    env_url = _normalise_base_url(os.environ.get(_PUBLIC_BASE_URL_ENV, ""))
    if env_url:
        return env_url
    cached = get_openapi_public_base_url()
    return str(cached.get("base_url") or "")

