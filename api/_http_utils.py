"""Shared HTTP/auth/validation utilities for Azure Functions HTTP triggers.

Extracted from the monolithic ``function_app.py`` so route Blueprints can
import them without depending on the entry point.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

import azure.functions as func

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
