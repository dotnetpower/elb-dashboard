"""``/api/aks/*`` route package."""

from __future__ import annotations

from fastapi import APIRouter

from api.routes.aks import lifecycle as _lifecycle_routes
from api.routes.aks import openapi as _openapi_routes
from api.routes.aks import provision as _provision_routes
from api.routes.aks import roles as _roles_routes
from api.routes.aks import skus as _skus_routes
from api.routes.aks.common import _invalidate_aks_monitor_cache as _invalidate_aks_monitor_cache
from api.routes.aks.lifecycle import (
    aks_delete as aks_delete,
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
    aks_openapi_proxy as aks_openapi_proxy,
)
from api.routes.aks.openapi import (
    aks_openapi_spec as aks_openapi_spec,
)
from api.routes.aks.provision import aks_provision as aks_provision
from api.routes.aks.roles import aks_assign_roles as aks_assign_roles
from api.routes.aks.skus import aks_skus as aks_skus

aks_router = APIRouter(prefix="/api/aks", tags=["aks"])
aks_router.include_router(_skus_routes.router)
aks_router.include_router(_provision_routes.router)
aks_router.include_router(_openapi_routes.router)
aks_router.include_router(_lifecycle_routes.router)
aks_router.include_router(_roles_routes.router)
