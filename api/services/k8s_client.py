"""Compatibility wrapper for `api.services.k8s.client`.

Responsibility: Compatibility wrapper for `api.services.k8s.client`
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: Module import side effects and constants.
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py`.
"""

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
