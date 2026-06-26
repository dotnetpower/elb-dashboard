"""Deployment-wide outbound webhook config + SSRF-safe URL validation.

Responsibility: Persist the single per-deployment webhook notification config
(URL, enabled, events) in one Azure Table row, and validate a webhook URL against
an SSRF allowlist (https-only + known webhook hosts) before it is ever stored or
called. The stored URL is a secret (it embeds a bearer-like token), so reads mask
it.
Edit boundaries: Azure-Tables access for the ``webhookpref`` table + the URL
guard live here. No HTTP send (that is ``api/tasks/webhooks.py``), no message
shaping.
Key entry points: ``get_config``, ``save_config``, ``validate_webhook_url``,
``mask_url``.
Risky contracts: ``validate_webhook_url`` is the ONLY SSRF gate — it rejects
non-https, IP-literal hosts, and any host not under the allowlist
(``hooks.slack.com`` / ``*.webhook.office.com`` / Discord, extendable via
``WEBHOOK_ALLOWED_HOSTS``). Loosening it re-opens SSRF. The config row is
deployment-wide (PK/RK fixed); the send path is separately gated by
``WEBHOOK_NOTIFICATIONS_ENABLED``.
Validation: ``uv run pytest -q api/tests/test_webhooks_pref.py``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

LOGGER = logging.getLogger(__name__)

_TABLE_NAME = "webhookpref"
_PARTITION = "webhook"
_ROW_KEY = "current"
_MAX_URL_LEN = 600

# Known incoming-webhook hosts. A leading dot means "any subdomain of"; an exact
# host matches only itself. Extend per-deployment via WEBHOOK_ALLOWED_HOSTS.
_ALLOWED_HOSTS: tuple[str, ...] = (
    "hooks.slack.com",
    ".webhook.office.com",
    ".webhook.office365.us",
    "discord.com",
    "discordapp.com",
    ".logic.azure.com",
)

_VALID_EVENTS = frozenset({"terminal", "failed_only"})

_TABLE_POOL: _PooledTableClient | None = None
_TABLE_POOL_LOCK = Lock()


class WebhookValidationError(ValueError):
    """Raised when a webhook URL fails the SSRF allowlist / format check."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _extra_allowed_hosts() -> list[str]:
    raw = os.environ.get("WEBHOOK_ALLOWED_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for suffix in (*_ALLOWED_HOSTS, *_extra_allowed_hosts()):
        suffix = suffix.lower()
        if suffix.startswith("."):
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == suffix:
            return True
    return False


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_webhook_url(url: str) -> str:
    """Return the URL if it is an https allowlisted webhook, else raise.

    SSRF gate: rejects non-https, over-long, IP-literal hosts (which would bypass
    the domain allowlist), and any host not under the allowlist. An empty URL is
    allowed (it means "clear the config") and returned as "".
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_URL_LEN:
        raise WebhookValidationError("webhook URL is too long")
    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise WebhookValidationError("webhook URL must use https")
    host = parsed.hostname or ""
    if _is_ip_literal(host):
        raise WebhookValidationError("webhook URL must be a hostname, not an IP literal")
    if not _host_allowed(host):
        raise WebhookValidationError(
            "webhook host is not allowlisted (allowed: Slack / Teams / Discord / Logic Apps; "
            "extend with WEBHOOK_ALLOWED_HOSTS)"
        )
    return cleaned


def mask_url(url: str) -> str:
    """Mask the secret tail of a webhook URL for display (keep scheme+host+head)."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    host = parsed.hostname or ""
    path = parsed.path or ""
    # Keep the first path segment, mask the rest (the token-bearing tail).
    segments = [s for s in path.split("/") if s]
    if not segments:
        return f"https://{host}/***"
    head = segments[0]
    return f"https://{host}/{head}/***"


def _table_client() -> TableClient:
    global _TABLE_POOL
    pool = _TABLE_POOL
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _TABLE_POOL_LOCK:
        if _TABLE_POOL is None:
            _TABLE_POOL = _PooledTableClient(
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _TABLE_POOL  # type: ignore[return-value]


def _reset_table_pool() -> None:
    """Test hook + credential-reset safety valve."""
    global _TABLE_POOL
    with _TABLE_POOL_LOCK:
        pool = _TABLE_POOL
        _TABLE_POOL = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    cache_key = (endpoint, _TABLE_NAME)
    if cache_key in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if cache_key in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(cache_key)


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    enabled: bool
    events: str  # "terminal" | "failed_only"
    updated_at: str = ""

    def public_dict(self) -> dict[str, Any]:
        """Response shape — the URL is masked (it is a secret)."""
        return {
            "configured": bool(self.url),
            "url_masked": mask_url(self.url),
            "enabled": self.enabled,
            "events": self.events,
            "updated_at": self.updated_at,
        }


def _normalise_events(events: Any) -> str:
    value = str(events or "terminal").strip()
    return value if value in _VALID_EVENTS else "terminal"


def get_config() -> WebhookConfig | None:
    """Return the stored webhook config, or ``None`` when unset / on fault."""
    try:
        _ensure_table()
        with _table_client() as table:
            try:
                entity = dict(table.get_entity(partition_key=_PARTITION, row_key=_ROW_KEY))
            except ResourceNotFoundError:
                return None
        return WebhookConfig(
            url=str(entity.get("url") or ""),
            enabled=bool(entity.get("enabled")),
            events=_normalise_events(entity.get("events")),
            updated_at=str(entity.get("updated_at") or ""),
        )
    except Exception as exc:
        LOGGER.warning("webhook config read failed: %s", type(exc).__name__)
        return None


def save_config(*, url: str, enabled: bool, events: str, owner_oid: str = "") -> WebhookConfig:
    """Validate + persist the webhook config (last-writer-wins).

    Raises ``WebhookValidationError`` when a non-empty URL fails the SSRF
    allowlist. A BLANK URL means "keep the current URL" (so an operator can
    toggle enabled / change events without re-entering the secret, which is only
    ever shown masked); when there is no current URL, blank leaves it unset and
    forces ``enabled=False``. To stop notifications, set ``enabled=False``.
    """
    cleaned = (url or "").strip()
    if cleaned:
        validated = validate_webhook_url(cleaned)
    else:
        existing = get_config()
        validated = existing.url if existing else ""
    effective_enabled = bool(enabled) and bool(validated)
    norm_events = _normalise_events(events)
    _ensure_table()
    now = _now_iso()
    entity = {
        "PartitionKey": _PARTITION,
        "RowKey": _ROW_KEY,
        "url": validated,
        "enabled": effective_enabled,
        "events": norm_events,
        "owner_oid": owner_oid or "",
        "updated_at": now,
    }
    with _table_client() as table:
        table.upsert_entity(entity, mode=UpdateMode.REPLACE)
    return WebhookConfig(
        url=validated, enabled=effective_enabled, events=norm_events, updated_at=now
    )
