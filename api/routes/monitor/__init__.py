"""/api/monitor route package.

Responsibility: /api/monitor route package
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `package imports`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
api/tests/test_monitor_cache.py`.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routes.monitor import acr as _acr_routes
from api.routes.monitor import aks as _aks_routes
from api.routes.monitor import cluster as _cluster_routes
from api.routes.monitor import jobs as _jobs_routes
from api.routes.monitor import logs as _logs_routes
from api.routes.monitor import message_flow as _message_flow_routes
from api.routes.monitor import metrics as _metrics_routes
from api.routes.monitor import sidecars as _sidecars_routes
from api.routes.monitor import storage as _storage_routes
from api.routes.monitor import terminal as _terminal_routes
from api.routes.monitor.common import _cache_key as _cache_key
from api.routes.monitor.common import _graceful as _graceful
from api.routes.monitor.common import _sub_default as _sub_default
from api.routes.monitor.sidecars import (
    _SIDECAR_BROADCASTER as _SIDECAR_BROADCASTER,
)
from api.routes.monitor.sidecars import (
    _SSE_PUSH_INTERVAL_SEC as _SSE_PUSH_INTERVAL_SEC,
)
from api.routes.monitor.sidecars import (
    _SidecarBroadcaster as _SidecarBroadcaster,
)
from api.routes.monitor.sidecars import (
    collect_snapshot as collect_snapshot,
)
from api.services import get_credential as get_credential

router = APIRouter(tags=["monitor"])
router.include_router(_aks_routes.router)
router.include_router(_metrics_routes.router)
router.include_router(_storage_routes.router)
router.include_router(_acr_routes.router)
router.include_router(_terminal_routes.router)
router.include_router(_cluster_routes.router)
router.include_router(_jobs_routes.router)
router.include_router(_logs_routes.router)
router.include_router(_message_flow_routes.router)
router.include_router(_sidecars_routes.router)
