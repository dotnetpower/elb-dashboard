"""Task-backed `/api/blast/databases/{db}/oracle` dispatch route.

Responsibility: Validate and authorize manual DB order-oracle requests, open
    sanctioned local Storage access when applicable, resolve the container
    image, and shape the shared durable-dispatch result.
Edit boundaries: HTTP/auth/validation/response shaping only; readiness, Blob
    claims, JobState creation, broker enqueue, and Kubernetes execution live in
    `api.services.db.oracle_*` and `api.tasks.storage.oracle`.
Key entry points: `blast_database_order_oracle`, `OracleBuildRequest`.
Risky contracts: Every request enforces `require_caller`; `resource_group`
    scopes Storage while optional `aks_resource_group` scopes AKS; the route
    preserves legacy same-RG fallback and existing response fields.
Validation: `uv run pytest -q api/tests/test_blast_oracle_aks_route.py
    api/tests/test_oracle_dispatch.py api/tests/test_oracle_task.py`.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _maybe_open_local_storage_access
from api.routes.blast.databases import (
    _DB_NAME_RE,
    _RESOURCE_GROUP_RE,
    _STORAGE_ACCOUNT_RE,
    _SUBSCRIPTION_RE,
)
from api.services.db.oracle_build import OracleBuildBlocked
from api.services.db.oracle_state import OracleBuildInProgress, OracleStateConflict

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class OracleBuildRequest(BaseModel):
    """Validated manual oracle request with legacy aliases retained."""

    model_config = ConfigDict(extra="ignore")

    subscription_id: str = ""
    resource_group: str = ""
    aks_resource_group: str | None = None
    account_name: str | None = None
    storage_account: str | None = None
    cluster_name: str | None = None
    aks_cluster_name: str | None = None
    acr_name: str | None = None
    image: str | None = None
    source_version: str | None = None


def _http_blocked(exc: OracleBuildBlocked) -> HTTPException:
    detail: dict[str, object] = {
        "code": exc.code,
        "message": str(exc),
    }
    cluster_reason = str(exc.details.get("cluster_reason") or "")
    if cluster_reason in {"cluster_stopped", "cluster_not_found"}:
        detail["cluster_reason"] = cluster_reason
    power_state = str(exc.details.get("cluster_power_state") or "")
    if re.fullmatch(r"[A-Za-z]{1,32}", power_state):
        detail["cluster_power_state"] = power_state
    return HTTPException(status_code=exc.status_code, detail=detail)


@router.post("/databases/{db_name}/oracle")
def blast_database_order_oracle(
    db_name: str,
    body: OracleBuildRequest,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, object]:
    """Claim and enqueue a cached DB-order oracle build."""
    from api.services import get_credential
    from api.services.db.oracle_dispatch import start_oracle_build
    from api.services.image_tags import IMAGE_TAGS

    subscription_id = body.subscription_id.strip()
    storage_resource_group = body.resource_group.strip()
    cluster_resource_group = (body.aks_resource_group or storage_resource_group).strip()
    storage_account = (body.account_name or body.storage_account or "").strip()
    cluster_name = (body.cluster_name or body.aks_cluster_name or "").strip()
    acr_name = (body.acr_name or "").strip().lower()
    image = (body.image or "").strip()
    if not image and acr_name:
        image = f"{acr_name}.azurecr.io/ncbi/elb:{IMAGE_TAGS['ncbi/elb']}"
    if not all(
        [
            subscription_id,
            storage_resource_group,
            storage_account,
            cluster_name,
            image,
        ]
    ):
        raise HTTPException(
            400,
            (
                "subscription_id, resource_group, account_name, cluster_name, "
                "and acr_name or image required"
            ),
        )
    if not _DB_NAME_RE.fullmatch(db_name):
        raise HTTPException(400, "invalid db_name")
    if not _SUBSCRIPTION_RE.fullmatch(subscription_id):
        raise HTTPException(400, "invalid subscription_id")
    if not _RESOURCE_GROUP_RE.fullmatch(storage_resource_group):
        raise HTTPException(400, "invalid resource_group")
    if not _RESOURCE_GROUP_RE.fullmatch(cluster_resource_group):
        raise HTTPException(400, "invalid aks_resource_group")
    if not _STORAGE_ACCOUNT_RE.fullmatch(storage_account):
        raise HTTPException(400, "invalid account_name")

    credential = get_credential()
    from api.services.auto_oracle_reconcile import (
        oracle_caller_write_authorized,
    )

    authorized, _reason = oracle_caller_write_authorized(
        credential,
        caller_oid=caller.object_id,
        subscription_id=subscription_id,
        cluster_resource_group=cluster_resource_group,
        cluster_name=cluster_name,
        storage_resource_group=storage_resource_group,
    )
    if not authorized:
        raise HTTPException(
            403,
            {
                "code": "oracle_build_permission_denied",
                "message": (
                    "AKS and Storage write permissions are required to build an order oracle."
                ),
            },
        )
    _maybe_open_local_storage_access(
        credential,
        subscription_id,
        storage_resource_group,
        storage_account,
        context="blast_database_order_oracle",
    )
    try:
        result = start_oracle_build(
            credential,
            subscription_id=subscription_id,
            storage_resource_group=storage_resource_group,
            storage_account=storage_account,
            cluster_resource_group=cluster_resource_group,
            cluster_name=cluster_name,
            db_name=db_name,
            image=image,
            requested_source_version=(body.source_version or "").strip(),
            owner_oid=caller.object_id,
            tenant_id=caller.tenant_id,
            automatic=False,
        )
    except OracleBuildBlocked as exc:
        raise _http_blocked(exc) from exc
    except OracleBuildInProgress as exc:
        raise HTTPException(
            409,
            {"code": "oracle_build_in_progress", "message": str(exc)},
        ) from exc
    except OracleStateConflict as exc:
        raise HTTPException(
            409,
            {"code": "oracle_state_conflict", "message": str(exc)},
        ) from exc
    except Exception as exc:
        LOGGER.warning(
            "db-order oracle dispatch failed db=%s reason=%s",
            db_name,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            {
                "code": "oracle_dispatch_failed",
                "message": "Order oracle dispatch failed; retry shortly.",
            },
        ) from exc

    response: dict[str, object] = {
        "accepted": result.accepted,
        "status": result.status,
        "db_name": result.db_name,
        "run_id": result.run_id,
        "job_id": result.job_id,
        "task_id": result.task_id,
        "expected_parts": result.expected_parts,
        "status_blob": result.status_blob,
        "part_prefix": result.part_prefix,
        "adopted": result.adopted,
    }
    # Preserve the synchronous route's legacy arrays while execution is now
    # task-backed. URLs remain empty so no browser Storage access is introduced.
    response.update({"created": [], "existing": [], "part_urls": []})
    return response
