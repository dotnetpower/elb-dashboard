"""Key Vault secret helpers."""

from __future__ import annotations

import logging

from azure.core.credentials import TokenCredential

from services.azure_clients import kv_secret_client

LOGGER = logging.getLogger(__name__)


def store_secret(
    credential: TokenCredential,
    vault_uri: str,
    name: str,
    value: str,
    tags: dict[str, str] | None = None,
) -> str:
    """Set or update a secret. Returns the secret's full id (including version)."""
    LOGGER.info("store_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    secret = client.set_secret(name, value, tags=tags)
    return secret.id or ""


def get_secret(credential: TokenCredential, vault_uri: str, name: str) -> str:
    """Read the latest version of a secret."""
    LOGGER.info("get_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    return client.get_secret(name).value or ""


def delete_secret(credential: TokenCredential, vault_uri: str, name: str) -> None:
    LOGGER.info("delete_secret name=%s vault=%s", name, vault_uri)
    client = kv_secret_client(credential, vault_uri)
    client.begin_delete_secret(name).wait()
