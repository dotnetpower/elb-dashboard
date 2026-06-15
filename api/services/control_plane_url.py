"""Control plane public URL setting (single deployment-wide value).

Persist and resolve the dashboard's own public base URL — the custom domain an
operator binds to the control-plane Container App (e.g.
``https://dashboard.elasticblast.com``). The ElasticBLAST OpenAPI sibling
webhooks back to this URL (``CONTROL_PLANE_URL``); when an operator configures a
custom domain here the OpenAPI deploy injects it instead of the auto-generated
``*.azurecontainerapps.io`` FQDN.

Responsibility: Validate, persist (durable ``dashboardsingletons`` Table via the
    singleton store), read, clear, and resolve (env -> settings -> Container App
    default) the single deployment-wide control-plane public URL.
Edit boundaries: Reusable domain/persistence logic only. HTTP shaping lives in
    ``api.routes.settings.control_plane``; the OpenAPI deploy task imports
    ``resolve_control_plane_url`` from here. No Azure management / data-plane SDK
    calls.
Key entry points: ``normalise_control_plane_url``, ``save_control_plane_url``,
    ``get_control_plane_url``, ``clear_control_plane_url``,
    ``container_app_default_url``, ``resolve_control_plane_url``.
Risky contracts: The sibling enforces ``https://`` (only ``localhost`` is
    exempt), so ``normalise_control_plane_url`` rejects non-localhost ``http://``
    and any URL carrying a path / query / fragment so a stray ``/api`` suffix
    cannot corrupt the webhook target. A missing setting reads back as an empty
    string (charter §12a Rule 4: unset = existing behaviour preserved, i.e. the
    Container App default FQDN). Durable-only by design — read at OpenAPI deploy
    time + Settings GET (low frequency), so no Redis hot path.
Validation: ``uv run pytest -q api/tests/test_settings_control_plane.py``.
"""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

_SINGLETON_KEY = "control-plane:public-url"
_DASHBOARD_PUBLIC_URL_ENV = "DASHBOARD_PUBLIC_URL"

SOURCE_ENV = "env"
SOURCE_SETTINGS = "settings"
SOURCE_CONTAINER_APP = "container_app"
SOURCE_NONE = "none"

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1"})


def normalise_control_plane_url(value: str) -> str:
    """Validate + normalise a control-plane public URL.

    Returns the trimmed URL with any trailing slash removed, or ``""`` for an
    empty input. Enforces the sibling's scheme contract: ``https://`` is
    required except for a ``localhost`` / ``127.0.0.1`` host (dev), and the URL
    must be an origin (scheme + host[:port]) with no path / query / fragment so a
    stray ``/api`` suffix cannot corrupt the webhook target.

    Raises:
        ValueError: the URL is malformed, uses a non-localhost ``http://``
            scheme, is missing a host, or carries a path / query / fragment.
    """
    url = (value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("URL must start with https://")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("URL must include a hostname")
    if parsed.scheme == "http" and host not in _LOCALHOST_HOSTS:
        raise ValueError("URL must use https:// (http is allowed only for localhost)")
    if parsed.path not in ("", "/"):
        raise ValueError("URL must not include a path")
    if parsed.query or parsed.fragment:
        raise ValueError("URL must not include a query or fragment")
    return url


def _normalise_stored(value: str) -> str:
    """Light normalisation for a value read back from the durable store.

    Unlike :func:`normalise_control_plane_url` this never raises — a row written
    by a prior (validated) save is trusted, so we only trim + strip the trailing
    slash.
    """
    return (value or "").strip().rstrip("/")


def save_control_plane_url(url: str) -> bool:
    """Validate + persist the control-plane public URL durably.

    Returns ``False`` when the durable write fails (no Table configured / SDK
    error) so the caller can surface a degraded save instead of pretending the
    value stuck.

    Raises:
        ValueError: ``url`` fails :func:`normalise_control_plane_url` or is empty.
    """
    normalised = normalise_control_plane_url(url)
    if not normalised:
        raise ValueError("URL must not be empty")
    from api.services.state.singletons import save_singleton

    payload = {
        "base_url": normalised,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ok = bool(save_singleton(_SINGLETON_KEY, payload))
    if not ok:
        LOGGER.warning("control plane url durable save returned False")
    return ok


def get_control_plane_url() -> str:
    """Return the operator-configured control-plane public URL, or ``""``.

    Best-effort: any durable-store failure degrades to ``""`` so a missing /
    unreachable Table never breaks the OpenAPI deploy resolution (it just falls
    through to the Container App default FQDN).
    """
    try:
        from api.services.state.singletons import load_singleton

        payload = load_singleton(_SINGLETON_KEY) or {}
    except Exception as exc:
        LOGGER.debug("control plane url load failed: %s", type(exc).__name__)
        return ""
    return _normalise_stored(str(payload.get("base_url") or ""))


def clear_control_plane_url() -> bool:
    """Drop the operator-configured control-plane public URL. Best-effort."""
    try:
        from api.services.state.singletons import clear_singleton

        return bool(clear_singleton(_SINGLETON_KEY))
    except Exception as exc:
        LOGGER.debug("control plane url clear failed: %s", type(exc).__name__)
        return False


def container_app_default_url() -> str:
    """Return the auto-generated Container Apps FQDN URL, or ``""``.

    ``CONTAINER_APP_NAME`` + ``CONTAINER_APP_ENV_DNS_SUFFIX`` are injected by the
    Azure Container Apps runtime on every replica, so this is the production
    default when no custom domain is configured.
    """
    name = (os.environ.get("CONTAINER_APP_NAME") or "").strip()
    suffix = (os.environ.get("CONTAINER_APP_ENV_DNS_SUFFIX") or "").strip()
    if name and suffix:
        return f"https://{name}.{suffix}"
    return ""


def resolve_control_plane_url() -> tuple[str, str]:
    """Resolve the effective control-plane URL and its source.

    Precedence (highest first):
      1. ``DASHBOARD_PUBLIC_URL`` env — deploy-time hard pin (tests / private DNS
         / custom hosts that set it explicitly).
      2. Operator-configured Settings value (custom domain).
      3. Container Apps auto-generated FQDN.
      4. ``""`` — the sibling's ``_webhook_notify`` becomes a no-op.

    Returns ``(url, source)`` where ``source`` is one of ``env`` / ``settings`` /
    ``container_app`` / ``none``. Always ``https://`` (or empty) — the env path
    only strips a trailing slash; the settings path is validated on write.
    """
    override = (os.environ.get(_DASHBOARD_PUBLIC_URL_ENV) or "").strip()
    if override:
        return override.rstrip("/"), SOURCE_ENV
    configured = get_control_plane_url()
    if configured:
        return configured, SOURCE_SETTINGS
    default = container_app_default_url()
    if default:
        return default, SOURCE_CONTAINER_APP
    return "", SOURCE_NONE
