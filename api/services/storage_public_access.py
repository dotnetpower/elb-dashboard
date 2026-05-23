"""Compatibility wrapper for `api.services.storage.public_access`.

Responsibility: Re-export `api.services.storage.public_access` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.storage.public_access`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_storage_public_access.py`.
"""

from api.services.storage.public_access import (
    ensure_local_storage_access,
    is_local_debug_auto_open_enabled,
    is_running_locally,
    read_local_storage_state,
)

__all__ = [
    "ensure_local_storage_access",
    "is_local_debug_auto_open_enabled",
    "is_running_locally",
    "read_local_storage_state",
]
