"""Storage monitor routes.

Responsibility: Storage monitor routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `storage_summary`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _cache_key, _graceful, _sub_default
from api.services import monitoring as monitoring_svc
from api.services.monitor_cache import cached_snapshot

router = APIRouter()


@router.get("/storage")
def storage_summary(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    account_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    sub = subscription_id or _sub_default()
    from api.routes import monitor as monitor_package

    cred = monitor_package.get_credential()
    try:
        return cached_snapshot(
            _cache_key("monitor", "storage", sub, resource_group, account_name),
            lambda: monitoring_svc.get_storage_summary(cred, sub, resource_group, account_name),
        )
    except Exception as exc:
        return cast(
            dict[str, Any],
            _graceful("storage_summary", exc, empty={"name": account_name, "containers": []}),
        )


# ---------------------------------------------------------------------------
# AKS run-command — proxy kubectl commands via Kubernetes API
# ---------------------------------------------------------------------------
