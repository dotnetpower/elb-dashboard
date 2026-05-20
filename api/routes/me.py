"""Caller identity endpoint. Returns the validated token's `oid`/`tid`/`upn`.

Responsibility: Caller identity endpoint. Returns the validated token's `oid`/`tid`/`upn`
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `me`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.auth import CallerIdentity, require_caller

router = APIRouter(tags=["identity"])


@router.get("/me")
def me(caller: CallerIdentity = Depends(require_caller)) -> dict[str, str | None]:
    """Return the validated caller's identity claims.

    Mirrors the Function App's `GET /api/me`.
    """
    return {
        "object_id": caller.object_id,
        "tenant_id": caller.tenant_id,
        "upn": caller.upn,
    }
