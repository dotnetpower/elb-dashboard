"""/api/aks/*`` route package.

Responsibility: /api/aks/*`` route package
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `package imports`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routes.aks import autostop as _autostop_routes
from api.routes.aks import cancel as _cancel_routes
from api.routes.aks import ensure_running as _ensure_running_routes
from api.routes.aks import lifecycle as _lifecycle_routes
from api.routes.aks import openapi as _openapi_routes
from api.routes.aks import openapi_databases as _openapi_databases_routes
from api.routes.aks import openapi_proxy as _openapi_proxy_routes
from api.routes.aks import peering as _peering_routes
from api.routes.aks import preflight as _preflight_routes
from api.routes.aks import provision as _provision_routes
from api.routes.aks import recent_failures as _recent_failures_routes
from api.routes.aks import roles as _roles_routes
from api.routes.aks import skus as _skus_routes
from api.routes.aks.common import _invalidate_aks_monitor_cache as _invalidate_aks_monitor_cache
from api.routes.aks.ensure_running import (
    aks_openapi_ensure_running as aks_openapi_ensure_running,
)
from api.routes.aks.lifecycle import (
    aks_delete as aks_delete,
)
from api.routes.aks.lifecycle import (
    aks_scale as aks_scale,
)
from api.routes.aks.lifecycle import (
    aks_start as aks_start,
)
from api.routes.aks.lifecycle import (
    aks_stop as aks_stop,
)
from api.routes.aks.openapi import (
    aks_openapi_deploy as aks_openapi_deploy,
)
from api.routes.aks.openapi import (
    aks_openapi_deploy_status as aks_openapi_deploy_status,
)
from api.routes.aks.openapi import (
    aks_openapi_deployment as aks_openapi_deployment,
)
from api.routes.aks.openapi import (
    aks_openapi_pls as aks_openapi_pls,
)
from api.routes.aks.openapi import (
    aks_openapi_spec as aks_openapi_spec,
)
from api.routes.aks.openapi import (
    aks_openapi_token as aks_openapi_token,
)
from api.routes.aks.openapi import (
    aks_openapi_token_generate as aks_openapi_token_generate,
)
from api.routes.aks.openapi_databases import (
    aks_openapi_database as aks_openapi_database,
)
from api.routes.aks.openapi_databases import (
    aks_openapi_databases as aks_openapi_databases,
)
from api.routes.aks.openapi_proxy import (
    aks_openapi_proxy as aks_openapi_proxy,
)
from api.routes.aks.peering import aks_peer_with_platform as aks_peer_with_platform
from api.routes.aks.provision import aks_provision as aks_provision
from api.routes.aks.roles import aks_assign_roles as aks_assign_roles
from api.routes.aks.skus import aks_skus as aks_skus

aks_router = APIRouter(prefix="/api/aks", tags=["aks"])
aks_router.include_router(_skus_routes.router)
aks_router.include_router(_preflight_routes.router)
aks_router.include_router(_provision_routes.router)
aks_router.include_router(_cancel_routes.router)
aks_router.include_router(_recent_failures_routes.router)
aks_router.include_router(_openapi_routes.router)
aks_router.include_router(_openapi_databases_routes.router)
aks_router.include_router(_openapi_proxy_routes.router)
aks_router.include_router(_ensure_running_routes.router)
aks_router.include_router(_peering_routes.router)
aks_router.include_router(_lifecycle_routes.router)
aks_router.include_router(_roles_routes.router)
aks_router.include_router(_autostop_routes.router)
