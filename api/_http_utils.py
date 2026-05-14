"""Shared HTTP/auth/validation utilities for Azure Functions HTTP triggers.

Extracted from the monolithic ``function_app.py`` so route Blueprints can
import them without depending on the entry point.
"""

from __future__ import annotations

import ipaddress
import json
import logging as _logging
import os as _os
import re
from typing import Any

import azure.functions as func
from azure.core.credentials import TokenCredential as _TokenCredential
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)

from services.sanitise import sanitise

# ---------------------------------------------------------------------------
# Input validation patterns (Azure naming rules)
# ---------------------------------------------------------------------------
_RE_RESOURCE_GROUP = re.compile(r"^[-\w._()]{1,90}$")
_RE_VM_NAME = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}[a-zA-Z0-9]?$")
_RE_STORAGE_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_RE_ACR_NAME = re.compile(r"^[a-zA-Z0-9]{5,50}$")
_RE_CLUSTER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$")
_RE_DB_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
_RE_SUBSCRIPTION = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_BLOB_NAME = re.compile(r"^[^/][a-zA-Z0-9._/-]{0,1024}$")
_RE_INSTANCE_ID = re.compile(r"^[a-zA-Z0-9]{16,64}$")


def _validate_name(value: str, pattern: re.Pattern[str], label: str) -> str | None:
    """Return an error message if value doesn't match pattern, else None."""
    if not value:
        return f"{label} is required"
    if not pattern.match(value):
        return f"Invalid {label}: '{sanitise(value[:40])}'"
    return None


def _validate_ip(value: str) -> str | None:
    """Validate IPv4 address format."""
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        return f"Invalid IP address: '{sanitise(value[:40])}'"


def _validate_sub(value: str) -> str | None:
    """Validate subscription ID format."""
    return _validate_name(value, _RE_SUBSCRIPTION, "subscription_id")


def _validate_rg(value: str) -> str | None:
    """Validate resource group name format."""
    return _validate_name(value, _RE_RESOURCE_GROUP, "resource_group")


# ---------------------------------------------------------------------------
# Response helpers — apply security headers consistently
# ---------------------------------------------------------------------------
def _json_response(body: Any, status: int = 200) -> func.HttpResponse:
    resp = func.HttpResponse(
        json.dumps(body, default=str),
        status_code=status,
        mimetype="application/json; charset=utf-8",
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


def _error_response(status: int, message: str) -> func.HttpResponse:
    return _json_response({"error": message}, status=status)


def _azure_error_response(exc: Exception, *, operation: str) -> func.HttpResponse:
    """Return a sanitized, user-safe response for Azure SDK boundary failures."""
    if isinstance(exc, ResourceNotFoundError):
        return _error_response(404, f"{operation} not found")
    if isinstance(exc, ClientAuthenticationError):
        return _error_response(401, "Azure authentication failed")
    if isinstance(exc, (ServiceRequestError, ServiceResponseError)):
        return _error_response(503, f"{operation} temporarily unavailable")
    if isinstance(exc, HttpResponseError):
        status = exc.status_code or 502
        if status == 404:
            return _error_response(404, f"{operation} not found")
        if status in (408, 429) or status >= 500:
            return _error_response(503, f"{operation} temporarily unavailable")
        if 400 <= status < 500:
            return _error_response(status, sanitise(str(exc))[:300])
    return _error_response(502, f"{operation} failed: {sanitise(str(exc))[:300]}")


def _require_query(
    req: func.HttpRequest, *names: str
) -> tuple[dict[str, str] | None, func.HttpResponse | None]:
    """Extract required query params; return (values, None) or (None, 400 response)."""
    values: dict[str, str] = {}
    for name in names:
        v = req.params.get(name)
        if not v:
            return None, _error_response(400, f"missing query param '{name}'")
        values[name] = v
    return values, None


# ---------------------------------------------------------------------------
# Key Vault fallback helper — try multiple candidate vault URIs
# ---------------------------------------------------------------------------
_KV_LOGGER = _logging.getLogger(__name__)


def resolve_terminal_secret(
    credential: _TokenCredential,
    subscription_id: str,
    resource_group: str,
    vm_name: str,
    secret_name: str,
) -> tuple[str | None, str | None]:
    """Try to read *secret_name* from candidate Key Vaults.

    Returns ``(value, vault_uri)`` on success or ``(None, None)`` if all fail.

    Candidate order:
      1. ``KEY_VAULT_URI`` environment variable (prod KV).
      2. Canonical per-terminal KV derived from subscription/rg/vm_name.
      3. Legacy ``kv-elb-<suffix>`` pattern.
    """
    from services import keyvault as kv_svc

    candidate_uris: list[str] = []
    env_uri = _os.environ.get("KEY_VAULT_URI")
    if env_uri:
        candidate_uris.append(env_uri.rstrip("/") + "/")
    if subscription_id and resource_group:
        try:
            from activities.terminal import _default_vault_name

            canonical = _default_vault_name(subscription_id, resource_group, vm_name)
            canonical_uri = f"https://{canonical}.vault.azure.net/"
            if canonical_uri not in candidate_uris:
                candidate_uris.append(canonical_uri)
        except Exception as exc:
            _KV_LOGGER.warning("could not derive canonical vault name: %s", exc)
    legacy_suffix = vm_name[-8:] if len(vm_name) >= 8 else vm_name
    legacy_uri = f"https://kv-elb-{legacy_suffix}.vault.azure.net/"
    if legacy_uri not in candidate_uris:
        candidate_uris.append(legacy_uri)

    last_exc: Exception | None = None
    for vault_uri in candidate_uris:
        try:
            value = kv_svc.get_secret(credential, vault_uri, secret_name)
            return value, vault_uri
        except Exception as exc:
            last_exc = exc
            _KV_LOGGER.info("secret lookup miss on %s: %s", vault_uri, str(exc)[:120])
    _KV_LOGGER.warning(
        "secret %s not found in any candidate vault (%s): %s",
        secret_name,
        [u.split("//")[1].split(".")[0] for u in candidate_uris],
        last_exc,
    )
    return None, None
