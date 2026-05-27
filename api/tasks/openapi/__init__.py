"""Deploy ``elb-openapi`` to an existing AKS cluster (package facade).

Responsibility: Re-export the Celery task entry points so Celery's
    ``include=["api.tasks.openapi"]`` discovery still picks them up and existing callers
    (`api.routes.aks.openapi`) can continue to `from api.tasks.openapi import
    deploy_openapi_service` / `setup_openapi_public_https`.
Edit boundaries: Imports and re-exports only. The deploy pipeline lives in dedicated
    sibling modules (`constants.py`, `helpers.py`, `rbac.py`, `manifests.py`,
    `kubectl.py`, `deploy.py`, `public_https.py`).
Key entry points: `deploy_openapi_service`, `setup_openapi_public_https`,
    `disable_openapi_public_https`, `get_openapi_public_https_status`.
Risky contracts: The Celery task names `api.tasks.openapi.deploy_openapi_service`,
    `api.tasks.openapi.setup_openapi_public_https`, and
    `api.tasks.openapi.disable_openapi_public_https` are referenced by name in routes
    and the SPA — do not rename them when reshuffling.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

from api.tasks.openapi.deploy import deploy_openapi_service
from api.tasks.openapi.kubectl import kubectl_apply as _kubectl_apply
from api.tasks.openapi.manifests import build_manifests as _build_manifests
from api.tasks.openapi.public_https import (
    disable_openapi_public_https,
    get_openapi_public_https_status,
    setup_openapi_public_https,
)
from api.tasks.openapi.rbac import (
    assign_role_idempotent as _assign_role_idempotent,
)
from api.tasks.openapi.rbac import (
    setup_workload_identity as _setup_workload_identity,
)

__all__ = (
    "_assign_role_idempotent",
    "_build_manifests",
    "_kubectl_apply",
    "_setup_workload_identity",
    "deploy_openapi_service",
    "disable_openapi_public_https",
    "get_openapi_public_https_status",
    "setup_openapi_public_https",
)
