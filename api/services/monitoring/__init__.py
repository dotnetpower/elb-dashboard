"""Azure resource monitoring + provisioning subpackage (split from monitoring.py).

Responsibility: Group AKS/storage/ACR/VM/provisioning helpers under one namespace.
Edit boundaries: Submodules own behaviour; this `__init__` only aggregates exports.
Key entry points: see `__all__`.
Risky contracts: Keep Azure credentials centralized; sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests/test_monitoring_aks_pools.py`.
"""

from __future__ import annotations

# Re-export k8s.* symbols for callers that historically used
# `from api.services.monitoring import k8s_get_pods` etc.
from api.services.k8s.monitoring import (
    _get_k8s_session,
    k8s_cancel_blast_job,
    k8s_check_blast_status,
    k8s_check_namespace_exists,
    k8s_get_nodes,
    k8s_get_pods,
    k8s_get_service_ip,
    k8s_list_events,
    k8s_pod_logs,
    k8s_top_nodes,
    k8s_warmup_status,
)
from api.services.monitoring.acr import list_acr_repositories
from api.services.monitoring.aks import (
    BLAST_POOL_NAME,
    list_aks_clusters,
)
from api.services.monitoring.provisioning import (
    ACR_PULL_ROLE_ID,
    STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID,
    ensure_acr,
    ensure_storage_account,
)
from api.services.monitoring.storage import (
    get_storage_summary,
    set_storage_public_access,
)
from api.services.monitoring.vm import get_vm_status

__all__ = [
    "ACR_PULL_ROLE_ID",
    "BLAST_POOL_NAME",
    "STORAGE_BLOB_DATA_CONTRIBUTOR_ROLE_ID",
    "_get_k8s_session",
    "ensure_acr",
    "ensure_storage_account",
    "get_storage_summary",
    "get_vm_status",
    "k8s_cancel_blast_job",
    "k8s_check_blast_status",
    "k8s_check_namespace_exists",
    "k8s_get_nodes",
    "k8s_get_pods",
    "k8s_get_service_ip",
    "k8s_list_events",
    "k8s_pod_logs",
    "k8s_top_nodes",
    "k8s_warmup_status",
    "list_acr_repositories",
    "list_aks_clusters",
    "set_storage_public_access",
]
