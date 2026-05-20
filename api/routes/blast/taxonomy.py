"""/api/blast taxonomy lookup routes.

Responsibility: /api/blast taxonomy lookup routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `blast_taxonomy_stub`, `blast_taxonomy_search`, `blast_taxonomy_detail`,
`blast_taxonomy_image`, `blast_taxonomy_tree`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _TAXONOMY_DETAIL_PATH,
    _TAXONOMY_IMAGE_NAME,
    _TAXONOMY_SEARCH_LIMIT,
    _TAXONOMY_SEARCH_QUERY,
    _TAXONOMY_TREE_PATH,
    _TAXONOMY_TREE_SIBLING_LIMIT,
    _WARMUP_RELEASE_CALLER,
    _stub_log,
)
from api.routes.blast.common import LAB_TOOL_PENDING

router = APIRouter()


@router.post("/taxonomy")
def blast_taxonomy_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/taxonomy")
    raise HTTPException(503, detail=LAB_TOOL_PENDING)


@router.get("/taxonomy/search")
def blast_taxonomy_search(
    q: str = _TAXONOMY_SEARCH_QUERY,
    limit: int = _TAXONOMY_SEARCH_LIMIT,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy import TaxonomySearchUnavailable, search_taxonomy

    del caller
    try:
        return search_taxonomy(q, limit=limit)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_query_invalid", "message": str(exc)},
        ) from exc
    except TaxonomySearchUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "taxonomy_lookup_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc


@router.get("/taxonomy/detail/{taxid}")
def blast_taxonomy_detail(
    taxid: int = _TAXONOMY_DETAIL_PATH,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy import TaxonomySearchUnavailable, fetch_taxonomy_detail

    del caller
    try:
        return fetch_taxonomy_detail(taxid)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_taxid_invalid", "message": str(exc)},
        ) from exc
    except TaxonomySearchUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "taxonomy_lookup_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc


@router.get("/taxonomy/image")
def blast_taxonomy_image(
    name: str = _TAXONOMY_IMAGE_NAME,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy_image import (
        TaxonomyImageUnavailable,
        fetch_taxonomy_image,
    )

    del caller
    try:
        return fetch_taxonomy_image(name)
    except TaxonomyImageUnavailable as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_image_invalid_name", "message": str(exc)},
        ) from exc


@router.get("/taxonomy/tree/{taxid}")
def blast_taxonomy_tree(
    taxid: int = _TAXONOMY_TREE_PATH,
    sibling_limit: int = _TAXONOMY_TREE_SIBLING_LIMIT,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy import TaxonomySearchUnavailable, fetch_taxonomy_tree

    del caller
    try:
        return fetch_taxonomy_tree(taxid, sibling_limit=sibling_limit)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_taxid_invalid", "message": str(exc)},
        ) from exc
    except TaxonomySearchUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "taxonomy_tree_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc
