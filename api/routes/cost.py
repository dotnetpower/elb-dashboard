"""``/api/cost`` - approximate cluster cost estimate + per-cluster budget guardrail.

Responsibility: HTTP shaping for the cost card. Reads the cluster snapshot
(node SKU / count / power state) + uptime, runs the approximate estimator, and
compares the projected monthly cost against a stored per-cluster budget. Also
exposes budget read/write.
Edit boundaries: No Azure SDK directly — cluster data comes from the monitoring
service wrapper, cost math from ``cost.estimate``, persistence from
``cost.budget_pref``. The estimate is explicitly approximate; never present it as
a bill.
Key entry points: ``get_cost``, ``get_budget_route``, ``put_budget_route``.
Risky contracts: Every route enforces ``require_caller``. ``GET /api/cost``
degrades to a ``degraded`` payload (never 500) when the cluster cannot be read so
the dashboard card stays renderable.
Validation: ``uv run pytest -q api/tests/test_cost_routes.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cost", tags=["cost"])


class BudgetBody(BaseModel):
    subscription_id: str = Field(..., min_length=1, max_length=64)
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=255)
    monthly_budget_usd: float = Field(..., ge=0)


def _uptime_seconds(last_started_at: str, running: bool) -> int | None:
    if not running or not last_started_at:
        return None
    try:
        dt = datetime.fromisoformat(last_started_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - dt).total_seconds()))


@router.get("")
def get_cost(
    subscription_id: str = Query(..., min_length=1, max_length=64),
    resource_group: str = Query(..., min_length=1, max_length=90),
    cluster_name: str = Query(..., min_length=1, max_length=255),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return an approximate cost estimate + budget comparison for one cluster."""
    try:
        from api.services import get_credential
        from api.services.cost.budget_pref import get_budget
        from api.services.cost.estimate import estimate_cluster_cost
        from api.services.monitoring import get_aks_cluster_snapshot

        snapshot = get_aks_cluster_snapshot(
            get_credential(), subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.warning("cost snapshot failed: %s", type(exc).__name__)
        return {"degraded": True, "reason": "cluster_unavailable"}

    if snapshot is None:
        return {"degraded": True, "reason": "cluster_not_found"}

    power_state = str(snapshot.get("power_state") or "")
    running = power_state.lower() == "running"
    node_sku = str(snapshot.get("node_sku") or "")
    node_count = int(snapshot.get("node_count") or 0)

    last_started_at = ""
    try:
        from api.services.auto_stop import get_auto_stop_preference

        pref = get_auto_stop_preference(subscription_id, resource_group, cluster_name)
        if pref is not None:
            last_started_at = str(getattr(pref, "last_started_at", "") or "")
    except Exception as exc:
        LOGGER.debug("cost uptime lookup skipped: %s", type(exc).__name__)

    uptime = _uptime_seconds(last_started_at, running)
    estimate = estimate_cluster_cost(
        node_sku=node_sku,
        node_count=node_count,
        uptime_seconds=uptime,
        running=running,
    )

    budget = get_budget(subscription_id, resource_group, cluster_name)
    budget_usd = budget.monthly_budget_usd if budget else 0.0
    warning: dict[str, Any] | None = None
    if budget_usd > 0:
        ratio = estimate.projected_monthly_usd / budget_usd if budget_usd else 0.0
        warning = {
            "over_budget": estimate.projected_monthly_usd > budget_usd,
            "ratio": round(ratio, 3),
        }

    return {
        "cluster": {
            "name": str(snapshot.get("name") or cluster_name),
            "power_state": power_state,
            "node_sku": node_sku,
            "node_count": node_count,
        },
        "estimate": estimate.as_dict(),
        "budget": {"monthly_budget_usd": budget_usd, "set": budget_usd > 0},
        "warning": warning,
    }


@router.get("/budget")
def get_budget_route(
    subscription_id: str = Query(..., min_length=1, max_length=64),
    resource_group: str = Query(..., min_length=1, max_length=90),
    cluster_name: str = Query(..., min_length=1, max_length=255),
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the stored budget for a cluster (0 when unset)."""
    from api.services.cost.budget_pref import get_budget

    budget = get_budget(subscription_id, resource_group, cluster_name)
    return {
        "monthly_budget_usd": budget.monthly_budget_usd if budget else 0.0,
        "set": bool(budget and budget.monthly_budget_usd > 0),
    }


@router.put("/budget")
def put_budget_route(
    body: BudgetBody,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Set (or clear, with 0) the monthly budget threshold for a cluster."""
    from api.services.cost.budget_pref import BudgetPreference, save_budget

    saved = save_budget(
        BudgetPreference(
            subscription_id=body.subscription_id,
            resource_group=body.resource_group,
            cluster_name=body.cluster_name,
            monthly_budget_usd=body.monthly_budget_usd,
            owner_oid=caller.object_id,
        )
    )
    return saved.as_dict()
