"""Settings → NCBI API key routes.

Responsibility: HTTP shaping for the optional NCBI E-utilities API key — read
the masked status and persist/clear the key. Persistence + masking live in
``api.services.ncbi_pref``; the key is consumed by ``api.services.ncbi``.
Edit boundaries: HTTP only — no NCBI calls, no persistence logic. Every route
enforces ``require_caller``.
Key entry points: ``get_status``, ``put_key``.
Risky contracts: The plaintext key is NEVER returned to the browser — only the
masked view (presence + last 4 chars + source). When ``NCBI_API_KEY`` is set in
the deployment env the stored key is ignored (``env`` wins) and ``env_locked``
is true so the SPA can disable the input.
Validation: ``uv run pytest -q api/tests/test_settings_ncbi.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.services.ncbi_pref import ncbi_settings_public, save_ncbi_api_key

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
def get_status(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the masked NCBI API key status (never the plaintext key)."""
    return {"config": ncbi_settings_public()}


@router.put("")
def put_key(
    body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Persist (or clear when empty) the NCBI API key. Returns the masked view."""
    raw = body.get("api_key", "")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(
            400,
            detail={"code": "invalid_api_key", "message": "api_key must be a string"},
        )
    try:
        masked = save_ncbi_api_key(raw, owner_oid=caller.object_id)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_api_key", "message": str(exc)}
        ) from exc
    LOGGER.info("ncbi api key updated has_key=%s", masked.get("has_key"))
    return {"config": masked}
