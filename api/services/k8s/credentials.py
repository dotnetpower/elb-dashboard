"""Compatibility wrappers around the lower-level Kubernetes client credential/session pool.

Responsibility: Bridge `api.services.azure_clients.aks_client` into the reusable
`api.services.k8s.client` session helpers while preserving the historical
`api.services.k8s.monitoring._get_k8s_session` import surface.
Edit boundaries: Session/credential delegation only. Kubernetes API calls belong
in monitoring/status/manifests modules.
Key entry points: `_get_k8s_session`, `_get_k8s_credential_material`,
`reset_k8s_credential_cache`, `reset_k8s_session_pool`.
Risky contracts: Temporarily swapping `_k8s_client.aks_client` must always be
restored in a `finally` block so tests and callers do not leak patched clients.
Validation: `uv run pytest -q api/tests/test_k8s_blast_status.py`.
"""

from __future__ import annotations

from typing import Any

from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client
from api.services.k8s import client as _k8s_client


def reset_k8s_credential_cache() -> None:
    _k8s_client.reset_k8s_credential_cache()


def reset_k8s_session_pool() -> None:
    """Drop all pooled K8s sessions. Test-only re-export of the client helper."""
    _k8s_client.reset_k8s_session_pool()


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool = False,
) -> tuple[Any, str]:
    original = _k8s_client.aks_client
    _k8s_client.aks_client = aks_client
    try:
        return _k8s_client._get_k8s_session(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            admin=admin,
        )
    finally:
        _k8s_client.aks_client = original


def _get_k8s_credential_material(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool,
) -> Any:
    original = _k8s_client.aks_client
    _k8s_client.aks_client = aks_client
    try:
        return _k8s_client._get_k8s_credential_material(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            admin=admin,
        )
    finally:
        _k8s_client.aks_client = original
