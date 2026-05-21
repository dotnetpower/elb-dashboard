"""AKS SKU routes.

Responsibility: AKS SKU routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `aks_skus`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.services.aks_skus import SkuListResponse, sku_list_response

router = APIRouter()


@router.get("/skus")
def aks_skus(
    location: str = Query(default="koreacentral"),
    caller: CallerIdentity = Depends(require_caller),
) -> SkuListResponse:
    # Source-of-truth lives in api.services.aks_skus, which mirrors the
    # sibling repo's elastic_blast.azure_traits.AZURE_HPC_MACHINES allow-list.
    # Picking anything outside this list makes elastic-blast raise
    # NotImplementedError("Cannot get properties for ...") at submit time, so
    # the SPA dropdown must source its options from here.
    #
    # `degraded` stays True until a Celery task replaces this with a live
    # Microsoft.Compute/skus query that intersects with the allow-list and
    # filters by region availability. The static list is correct for the
    # SKU set elastic-blast understands; what's missing is per-region
    # availability and quota.
    return sku_list_response()
