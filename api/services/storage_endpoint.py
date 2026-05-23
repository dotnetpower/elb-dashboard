"""Compatibility wrapper for `api.services.storage.endpoint`.

Responsibility: Re-export `api.services.storage.endpoint` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.storage.endpoint`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from api.services.storage.endpoint import (
    azure_storage_suffix,
    blob_account_url,
    blob_host_for_account,
    dfs_account_url,
    queue_account_url,
    table_account_url,
)

__all__ = [
    "azure_storage_suffix",
    "blob_account_url",
    "blob_host_for_account",
    "dfs_account_url",
    "queue_account_url",
    "table_account_url",
]
