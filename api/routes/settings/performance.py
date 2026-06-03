"""Settings → Performance routes (per-cluster warm-cache mode).

Responsibility: HTTP shaping for reading and writing the per-cluster
    ``warm_cache_mode`` Performance preference. Validation + response shaping only.
Edit boundaries: No persistence logic here — that lives in
    ``api.services.performance_pref``. No Azure SDK calls.
Key entry points: ``get_performance``, ``put_performance``.
Risky contracts: Every route enforces ``require_caller``. ``warm_cache_mode`` is a
    closed enum validated by ``WarmCacheMode``; the GET returns the default
    ``ephemeral`` mode (never 404) when no row exists so the SPA can render a
    consistent control.
Validation: ``uv run pytest -q api/tests/test_settings_performance.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services.performance_pref import (
    DEFAULT_WARM_CACHE_MODE,
    get_performance_preference,
    normalise_preference,
    save_performance_preference,
)
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$")

WarmCacheMode = Literal["ephemeral", "node_disk", "data_disk"]


class PerformancePreferenceBody(BaseModel):
    subscription_id: str = Field(..., min_length=36, max_length=36)
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=255)
    warm_cache_mode: WarmCacheMode


def _validate(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.match(value):
        raise HTTPException(400, f"invalid {label}: '{sanitise(value[:80])}'")
    return value


@router.get("")
def get_performance(
    subscription_id: str = Query(...),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    """Return the per-cluster warm-cache mode. Defaults to ``ephemeral`` (current
    behaviour) when no preference row exists — never 404s so the SPA control has a
    stable value to render."""
    _validate(subscription_id, _RE_SUB, "subscription_id")
    _validate(resource_group, _RE_RG, "resource_group")
    _validate(cluster_name, _RE_NAME, "cluster_name")
    pref = get_performance_preference(subscription_id, resource_group, cluster_name)
    if pref is None:
        return {
            "preference": None,
            "warm_cache_mode": DEFAULT_WARM_CACHE_MODE,
        }
    return {
        "preference": pref.to_dict(),
        "warm_cache_mode": pref.warm_cache_mode,
    }


@router.put("")
def put_performance(
    body: PerformancePreferenceBody,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    _validate(body.subscription_id, _RE_SUB, "subscription_id")
    _validate(body.resource_group, _RE_RG, "resource_group")
    _validate(body.cluster_name, _RE_NAME, "cluster_name")
    try:
        pref = normalise_preference(
            {
                **body.model_dump(),
                "owner_oid": caller.object_id,
                "tenant_id": caller.tenant_id,
            }
        )
    except ValueError as exc:
        raise HTTPException(400, sanitise(str(exc))[:200]) from exc
    saved = save_performance_preference(pref)
    return {"status": "saved", "preference": saved.to_dict()}
