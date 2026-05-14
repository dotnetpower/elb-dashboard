"""api_app.services bridges back to the legacy `api/services/*` package.

The Function App backend already shipped a battle-tested set of Azure SDK
wrappers (azure_clients, monitoring, blast_config, sanitise, image_tags,
keyvault, storage_data, etc.). Rather than copy/paste them, the FastAPI
app re-exports the same module surface by adding `api/` to sys.path.

Phase B (this slice) reuses the wrappers as-is. Phase C will gradually
move the modules into `api_app/services/` proper so the legacy `api/`
directory can be deleted once the migration is complete.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `api/services/*` importable as plain `services.*`.
# `_APP_DIR` is `<repo>/api_app`; the legacy code lives at `<repo>/api`.
_LEGACY_API_DIR = Path(__file__).resolve().parent.parent.parent / "api"
if _LEGACY_API_DIR.is_dir():
    legacy_str = str(_LEGACY_API_DIR)
    if legacy_str not in sys.path:
        sys.path.insert(0, legacy_str)


def get_credential():
    """Return DefaultAzureCredential bound to the shared UAMI when running in
    Container Apps, or whatever azd login provides locally.

    Container Apps injects MSI_ENDPOINT/IDENTITY_ENDPOINT + AZURE_CLIENT_ID
    so DefaultAzureCredential picks the user-assigned identity automatically.
    """
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)
