"""Service-layer facade and shared Azure credential provider.

Responsibility: Service-layer facade and shared Azure credential provider
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `get_credential`, `reset_credential`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential

# Module-level singleton credential. Azure credential implementations are
# thread-safe and keep per-instance token caches, so creating multiple instances
# wastes both the instantiation cost and those token caches.
_CREDENTIAL: TokenCredential | None = None
_CREDENTIAL_LOCK = threading.Lock()


def _has_managed_identity_environment() -> bool:
    return bool(
        os.environ.get("CONTAINER_APP_NAME")
        or os.environ.get("IDENTITY_ENDPOINT")
        or os.environ.get("MSI_ENDPOINT")
        or os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    )


def get_credential() -> TokenCredential:
    """Return the singleton Azure credential.

    In Container Apps this resolves to the shared user-assigned MI
    `id-elb-dashboard-*` because Container Apps injects MSI_ENDPOINT /
    IDENTITY_ENDPOINT / AZURE_CLIENT_ID. Locally, when `AZURE_TENANT_ID` is set,
    it uses `AzureCliCredential(tenant_id=...)` so stale Azure Developer CLI
    tokens from another tenant cannot satisfy ARM or Storage requests.

    The credential is created lazily on first use and reused for the lifetime
    of the process. Token refresh is handled internally by the credential
    chain — callers do not need to invalidate or rebuild it.
    """
    global _CREDENTIAL
    if _CREDENTIAL is None:
        with _CREDENTIAL_LOCK:
            if _CREDENTIAL is None:
                tenant_id = os.environ.get("AZURE_TENANT_ID", "").strip()
                if tenant_id and not _has_managed_identity_environment():
                    from azure.identity import AzureCliCredential

                    _CREDENTIAL = AzureCliCredential(tenant_id=tenant_id)
                else:
                    from azure.identity import DefaultAzureCredential

                    _CREDENTIAL = DefaultAzureCredential(
                        exclude_developer_cli_credential=True,
                        exclude_interactive_browser_credential=True,
                    )
    return _CREDENTIAL


def reset_credential() -> None:
    """Drop the cached credential. Test-only — production code never needs this."""
    global _CREDENTIAL
    with _CREDENTIAL_LOCK:
        _CREDENTIAL = None
