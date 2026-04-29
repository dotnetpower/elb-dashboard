"""On-Behalf-Of (OBO) credential helpers.

The Function App exchanges the caller's incoming access token for downstream
tokens (ARM, Key Vault, etc.) so that every Azure mutation runs with the
caller's RBAC. This is the only credential path used by the API; no SP
secrets, no managed-identity-as-user.
"""

from __future__ import annotations

import os

from azure.identity import OnBehalfOfCredential


def caller_credential(user_assertion: str) -> OnBehalfOfCredential:
    """Return an OnBehalfOfCredential bound to the caller's access token.

    Requires API_CLIENT_ID + API_CLIENT_SECRET (or a federated credential
    configured separately). The downstream resource is determined by the
    SDK client that consumes this credential.
    """
    tenant_id = os.environ["AZURE_TENANT_ID"]
    client_id = os.environ["API_CLIENT_ID"]
    client_secret = os.environ.get("API_CLIENT_SECRET")
    if not client_secret:
        raise RuntimeError(
            "API_CLIENT_SECRET is not configured. Either set it in app settings "
            "or wire a federated credential and use a different code path."
        )
    return OnBehalfOfCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        user_assertion=user_assertion,
    )
