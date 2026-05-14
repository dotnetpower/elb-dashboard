"""Liveness endpoint. No auth required — used by Container Apps health probes."""

from __future__ import annotations

import os

from fastapi import APIRouter

from api import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Return 200 if the process can answer.

    Container Apps uses HTTP probes against this path. Keep the response
    cheap — no Azure SDK calls, no Storage reads, no token validation.
    """
    return {
        "status": "ok",
        "version": __version__,
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
    }
