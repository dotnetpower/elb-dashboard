"""Compatibility wrapper for `api.services.k8s.client`."""

from api.services.k8s.client import (
    _get_k8s_credential_material,
    _get_k8s_session,
    aks_client,
    reset_k8s_credential_cache,
)

__all__ = [
    "_get_k8s_credential_material",
    "_get_k8s_session",
    "aks_client",
    "reset_k8s_credential_cache",
]
