"""Shared helpers for /api/monitor route modules."""

from __future__ import annotations

import logging
import os
from typing import Any

from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError

from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)


def _sub_default() -> str:
    return os.environ.get("AZURE_SUBSCRIPTION_ID", "")


def _cache_key(*parts: object) -> str:
    return ":".join(str(part) for part in parts)


def _graceful(op: str, exc: Exception, *, empty: Any) -> Any:
    code: str
    if isinstance(exc, ResourceNotFoundError):
        code = "not_found"
    elif isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status == 403:
            code = "forbidden"
        elif status == 404:
            code = "not_found"
        else:
            code = f"http_{status or 'error'}"
    elif isinstance(exc, AzureError):
        code = "azure_error"
    else:
        code = type(exc).__name__
    LOGGER.warning("%s gracefully degraded: %s (%s)", op, code, sanitise(str(exc))[:200])
    out = dict(empty) if isinstance(empty, dict) else {"items": empty}
    out["degraded"] = True
    out["degraded_reason"] = code
    return out
