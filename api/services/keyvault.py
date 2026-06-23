"""Key Vault secret read helper.

Responsibility: Read a stored secret from Azure Key Vault for the small number
    of runtime callers that need one (currently the Service Bus SAS secret
    lookup in ``api.services.service_bus``). Vault provisioning and the
    access-policy / RBAC wiring are owned by the Bicep infra layer, not this
    module.
Edit boundaries: Keep reusable domain logic here; routes and tasks should call
    this layer instead of duplicating SDK code. Do NOT re-add vault
    provisioning / access-policy helpers — that path is handled declaratively
    in ``infra/`` and was retired with the Azure Functions backend.
Key entry points: `get_secret`
Risky contracts: Keep Azure credentials centralized and sanitise data before
    HTTP, WebSocket, or log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging

from azure.core.credentials import TokenCredential

from api.services.azure_clients import kv_secret_client

LOGGER = logging.getLogger(__name__)


def get_secret(credential: TokenCredential, vault_uri: str, name: str) -> str:
    """Read the latest version of a secret."""
    LOGGER.info("get_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    return client.get_secret(name).value or ""
