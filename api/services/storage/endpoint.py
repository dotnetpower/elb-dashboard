"""Centralised Azure Storage endpoint helpers.

Responsibility: Build the per-service account URLs (Blob / Table / DFS / Queue)
for the configured Azure cloud. Replaces the scattered
``f"https://{account}.blob.core.windows.net"`` string templates so a sovereign-cloud
deployment (US Gov, China) only needs to set one env var.
Edit boundaries: Keep this module pure (no Azure SDK imports). Callers depend on
``blob_account_url`` / ``blob_host_for_account`` returning stable values.
Key entry points: ``azure_storage_suffix``, ``blob_account_url``, ``table_account_url``,
``dfs_account_url``, ``blob_host_for_account``.
Risky contracts: The suffix is read from ``AZURE_STORAGE_SUFFIX`` and falls back to
``core.windows.net``. If a future deployment runs in a sovereign cloud, set the env var.
``account_name`` is the bare storage account name (no scheme, no host) — passing
``acct.blob.core.windows.net`` here would produce a malformed URL and is caller error.
Validation: ``uv run pytest -q api/tests/test_storage_endpoint.py``.
"""

from __future__ import annotations

import os

_DEFAULT_SUFFIX = "core.windows.net"
_ENV_SUFFIX = "AZURE_STORAGE_SUFFIX"


def azure_storage_suffix() -> str:
    """Return the Azure Storage hostname suffix for the configured cloud.

    Public Azure: ``core.windows.net``.
    US Gov: ``core.usgovcloudapi.net``.
    China: ``core.chinacloudapi.cn``.
    """
    return os.environ.get(_ENV_SUFFIX, _DEFAULT_SUFFIX).strip() or _DEFAULT_SUFFIX


def _validate_account_name(account_name: str) -> None:
    if not account_name or "/" in account_name or "." in account_name:
        # Catches the common mistake of passing a full host
        # (``acct.blob.core.windows.net``) or a partial URL.
        raise ValueError(
            "account_name must be a bare storage account name "
            f"(no scheme / no host components), got {account_name!r}"
        )


def blob_host_for_account(account_name: str) -> str:
    """``acct.blob.<suffix>`` — used by storage_url_validation expected-host checks."""
    _validate_account_name(account_name)
    return f"{account_name}.blob.{azure_storage_suffix()}"


def blob_account_url(account_name: str) -> str:
    """``https://acct.blob.<suffix>`` — used by ``BlobServiceClient(account_url=…)``."""
    return f"https://{blob_host_for_account(account_name)}"


def table_account_url(account_name: str) -> str:
    """``https://acct.table.<suffix>`` — used by ``TableServiceClient``."""
    _validate_account_name(account_name)
    return f"https://{account_name}.table.{azure_storage_suffix()}"


def dfs_account_url(account_name: str) -> str:
    """``https://acct.dfs.<suffix>`` — used by ADLS Gen2 ``DataLakeServiceClient``."""
    _validate_account_name(account_name)
    return f"https://{account_name}.dfs.{azure_storage_suffix()}"


def queue_account_url(account_name: str) -> str:
    """``https://acct.queue.<suffix>`` — kept for completeness, not used today."""
    _validate_account_name(account_name)
    return f"https://{account_name}.queue.{azure_storage_suffix()}"
