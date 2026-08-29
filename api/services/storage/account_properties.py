"""Read focused Azure Storage account capabilities for data-plane routing.

Responsibility: Resolve immutable Storage account capabilities needed to choose a safe
    Blob or ADLS Gen2 operation path.
Edit boundaries: Focused ARM property reads only; data-plane I/O, HTTP response shaping,
    public-network changes, and broad monitoring summaries stay in their owning modules.
Key entry points: `storage_hns_enabled`.
Risky contracts: Use the shared Azure client factory and never infer HNS from a feature
    flag; callers decide how to degrade when the ARM property read is unavailable.
Validation: `uv run pytest -q api/tests/test_prepare_db_delete_route.py`.
"""

from __future__ import annotations

from azure.core.credentials import TokenCredential

from api.services.azure_clients import storage_client


def storage_hns_enabled(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> bool:
    """Return whether the target Storage account uses hierarchical namespace."""
    account = storage_client(credential, subscription_id).storage_accounts.get_properties(
        resource_group,
        account_name,
    )
    return bool(account.is_hns_enabled)


__all__ = ["storage_hns_enabled"]
