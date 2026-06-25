"""``/api/blast/templates`` - per-user saved submit-option presets (CRUD).

Responsibility: HTTP validation + response shaping for the researcher's saved
submit templates. Persistence lives in ``api.services.blast.submit_templates``.
Edit boundaries: No Azure SDK here. The authenticated ``caller.object_id`` is the
only owner ever passed to the service — never a client-supplied owner id.
Key entry points: ``list_templates_route``, ``create_template_route``,
``update_template_route``, ``delete_template_route``.
Risky contracts: Every route enforces ``require_caller``. ``fields`` is an opaque
option snapshot (never the query data); the service caps its size and the per-user
count and raises ``TemplateValidationError`` (mapped to 400 here).
Validation: ``uv run pytest -q api/tests/test_blast_templates.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["blast"])

_ID_MAX_LEN = 64


class TemplateCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    fields: dict[str, Any] = Field(default_factory=dict)


class TemplateUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    fields: dict[str, Any] | None = None


@router.get("")
def list_templates_route(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the caller's saved submit templates."""
    from api.services.blast.submit_templates import list_templates

    templates = list_templates(caller.object_id)
    return {"templates": [t.as_dict() for t in templates]}


@router.post("", status_code=201)
def create_template_route(
    body: TemplateCreateBody,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Create a new submit template for the caller."""
    from api.services.blast.submit_templates import TemplateValidationError, create_template

    try:
        template = create_template(caller.object_id, body.name, body.fields)
    except TemplateValidationError as exc:
        raise HTTPException(400, sanitise(str(exc))[:200]) from exc
    return template.as_dict()


@router.put("/{template_id}")
def update_template_route(
    body: TemplateUpdateBody,
    template_id: str = Path(..., min_length=1, max_length=_ID_MAX_LEN, pattern=r"^[A-Za-z0-9_-]+$"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Update an existing template's name and/or fields."""
    from api.services.blast.submit_templates import TemplateValidationError, update_template

    try:
        template = update_template(
            caller.object_id, template_id, name=body.name, fields=body.fields
        )
    except TemplateValidationError as exc:
        raise HTTPException(400, sanitise(str(exc))[:200]) from exc
    if template is None:
        raise HTTPException(404, "template not found")
    return template.as_dict()


@router.delete("/{template_id}")
def delete_template_route(
    template_id: str = Path(..., min_length=1, max_length=_ID_MAX_LEN, pattern=r"^[A-Za-z0-9_-]+$"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Delete one of the caller's templates."""
    from api.services.blast.submit_templates import delete_template

    deleted = delete_template(caller.object_id, template_id)
    if not deleted:
        raise HTTPException(404, "template not found")
    return {"deleted": True}
