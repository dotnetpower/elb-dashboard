"""Azure infrastructure Celery tasks - AKS provision / start / stop / delete (facade).

Responsibility: Re-export the public Celery task entry points and the legacy helper
    aliases (`_build_cluster_params`, `_attach_acr`,
    `_grant_storage_blob_contributor_to_aks`) that routes, sibling tasks, and tests
    import directly from `api.tasks.azure`.
Edit boundaries: Imports and re-exports only. Each task / helper now lives in its own
    sibling module (`helpers.py`, `cluster_params.py`, `rbac.py`, `provision.py`,
    `lifecycle.py`, `diagnostics.py`).
Key entry points: `provision_aks`, `start_aks`, `stop_aks`, `delete_aks`,
    `assign_aks_roles`, `diag_noop`.
Risky contracts: Tests monkeypatch attributes on this package
    (`api.tasks.azure.start_aks.delay`, `api.tasks.azure.assign_aks_roles.delay`) and
    `api.tasks.storage.warmup` imports `_attach_acr` and
    `_grant_storage_blob_contributor_to_aks` from this package — those names must
    remain importable here.
Validation: `uv run pytest -q api/tests/test_azure_provision_aks.py
    api/tests/test_azure_tasks.py api/tests/test_warmup_route.py`.
"""

from __future__ import annotations

from api.services import get_credential
from api.services.azure_clients import (
    acr_client,
    aks_client,
    storage_client,
)
from api.tasks.azure.cluster_params import build_cluster_params as _build_cluster_params
from api.tasks.azure.diagnostics import diag_noop
from api.tasks.azure.helpers import (
    now_iso as _now_iso,
)
from api.tasks.azure.helpers import (
    update_state as _update_state,
)
from api.tasks.azure.lifecycle import delete_aks, start_aks, stop_aks
from api.tasks.azure.provision import provision_aks
from api.tasks.azure.rbac import (
    assign_aks_roles,
)
from api.tasks.azure.rbac import (
    attach_acr as _attach_acr,
)
from api.tasks.azure.rbac import (
    ensure_aks_runtime_rbac as _ensure_aks_runtime_rbac,
)
from api.tasks.azure.rbac import (
    grant_storage_blob_contributor_to_aks as _grant_storage_blob_contributor_to_aks,
)

__all__ = (
    "_attach_acr",
    "_build_cluster_params",
    "_ensure_aks_runtime_rbac",
    "_grant_storage_blob_contributor_to_aks",
    "_now_iso",
    "_update_state",
    "acr_client",
    "aks_client",
    "assign_aks_roles",
    "delete_aks",
    "diag_noop",
    "get_credential",
    "provision_aks",
    "start_aks",
    "stop_aks",
    "storage_client",
)
