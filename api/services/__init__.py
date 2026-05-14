"""api.services — Azure SDK wrappers used by the api / worker / beat sidecars.

This package is the **only** place in the active codebase that imports from
`azure.mgmt.*`, `azure.identity`, `azure.storage.blob`, `azure.data.tables`,
`azure.keyvault.*`, or `kubernetes`. Routes (`api.routes.*`) and Celery tasks
(`api.tasks.*`) MUST go through these wrappers so that:

  * Credential acquisition is centralised on `get_credential()` below
    (DefaultAzureCredential bound to the shared user-assigned MI
    `id-elb-control` in production).
  * Output sanitisation (`api.services.sanitise.sanitise`) wraps every value
    that crosses the HTTP / WebSocket / log boundary.
  * Storage policy is enforced in one place (see `storage_data.py` for the
    SAS-issuer ban — `.github/copilot-instructions.md` §9).

Modules: aks_skus, azure_clients, blast_config, image_tags, keyvault,
monitoring, network, passwords, sanitise, state_repo, storage_data,
terminal_exec.
"""

from __future__ import annotations

import threading

# Module-level singleton credential. DefaultAzureCredential is thread-safe and
# does its own internal token caching across the chain (managed identity →
# environment → az CLI → …) so creating multiple instances wastes both the
# instantiation cost and the per-instance token caches.
_CREDENTIAL: object | None = None
_CREDENTIAL_LOCK = threading.Lock()


def get_credential():
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
