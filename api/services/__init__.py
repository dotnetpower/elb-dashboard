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

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.identity import DefaultAzureCredential

# Module-level singleton credential. DefaultAzureCredential is thread-safe and
# does its own internal token caching across the chain (managed identity →
# environment → az CLI → …) so creating multiple instances wastes both the
# instantiation cost and the per-instance token caches.
_CREDENTIAL: DefaultAzureCredential | None = None
_CREDENTIAL_LOCK = threading.Lock()


def get_credential() -> DefaultAzureCredential:
    """Return the singleton DefaultAzureCredential.

    In Container Apps this resolves to the shared user-assigned MI
    `id-elb-control` because Container Apps injects MSI_ENDPOINT /
    IDENTITY_ENDPOINT / AZURE_CLIENT_ID. Locally it picks whatever
    `DefaultAzureCredential` finds (typically `az login`).

    The credential is created lazily on first use and reused for the lifetime
    of the process. Token refresh is handled internally by the credential
    chain — callers do not need to invalidate or rebuild it.
    """
    global _CREDENTIAL
    if _CREDENTIAL is None:
        with _CREDENTIAL_LOCK:
            if _CREDENTIAL is None:
                from azure.identity import DefaultAzureCredential

                _CREDENTIAL = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                )
    return _CREDENTIAL


def reset_credential() -> None:
    """Drop the cached credential. Test-only — production code never needs this."""
    global _CREDENTIAL
    with _CREDENTIAL_LOCK:
        _CREDENTIAL = None
