"""/api/settings/* route package.

Responsibility: Top-level router that aggregates Settings-panel HTTP routes
(App Insights provisioning, AKS Container Insights enablement).
Edit boundaries: Keep this `__init__.py` a thin aggregator. HTTP shaping for
each section lives in a sibling module.
Key entry points: `settings_router`.
Risky contracts: Every route under this prefix must enforce `require_caller`.
Validation: `uv run pytest -q api/tests/test_settings_app_insights.py
api/tests/test_settings_aks_observability.py api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routes.settings import aks_observability as _aks_observability_routes
from api.routes.settings import app_insights as _app_insights_routes
from api.routes.settings import control_plane as _control_plane_routes
from api.routes.settings import ncbi as _ncbi_routes
from api.routes.settings import openapi_token as _openapi_token_routes
from api.routes.settings import performance as _performance_routes
from api.routes.settings import service_bus as _service_bus_routes
from api.routes.settings import vnet_peering as _vnet_peering_routes
from api.routes.settings import webhooks as _webhooks_routes

settings_router = APIRouter(prefix="/api/settings", tags=["settings"])
settings_router.include_router(_app_insights_routes.router, prefix="/app-insights")
settings_router.include_router(
    _aks_observability_routes.router, prefix="/aks-observability"
)
settings_router.include_router(_control_plane_routes.router, prefix="/control-plane")
settings_router.include_router(_ncbi_routes.router, prefix="/ncbi")
settings_router.include_router(_openapi_token_routes.router, prefix="/openapi-token")
settings_router.include_router(_performance_routes.router, prefix="/performance")
settings_router.include_router(_service_bus_routes.router, prefix="/service-bus")
settings_router.include_router(_webhooks_routes.router, prefix="/webhooks")
settings_router.include_router(_vnet_peering_routes.router)
