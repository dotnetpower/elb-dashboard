"""Compatibility wrapper for `api.services.storage.network`.

Responsibility: Re-export `api.services.storage.network` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.storage.network`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_storage_network.py`.
"""

from api.services.storage.network import ensure_workload_storage_private_endpoints

__all__ = [
    "ensure_workload_storage_private_endpoints",
]
