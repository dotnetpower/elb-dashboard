"""``/api/acr/*`` — ACR image build."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay

LOGGER = logging.getLogger(__name__)

acr_build_router = APIRouter(prefix="/api/acr", tags=["acr"])


@acr_build_router.post("/build-images")
def acr_build_images(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.acr import build_images

    result = _safe_delay(
        build_images,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        registry_name=body.get("registry_name", ""),
        images=body.get("images"),
    )
    # For immediate feedback, return the expected images with "scheduled" status
    from api.services.image_tags import IMAGE_TAGS

    targets = body.get("images") or list(IMAGE_TAGS.keys())
    results = []
    for img in targets:
        tag = IMAGE_TAGS.get(img, "latest")
        results.append({"image": f"{img}:{tag}", "status": "scheduled"})
    return {"results": results, "task_id": result.id}

