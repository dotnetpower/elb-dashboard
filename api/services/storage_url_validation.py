"""Storage Blob URL validation helpers for API-generated runtime config.

Responsibility: Validate user-provided Azure Blob references against the selected
Storage account boundary.
Edit boundaries: Keep this module limited to pure URL parsing and validation; callers own
resource-specific policy decisions.
Key entry points: `absolute_blob_url`, `validate_storage_blob_reference`
Risky contracts: Do not allow SAS/query strings, cross-account hosts, or path traversal in
runtime config URLs.
Validation: `uv run pytest -q api/tests/test_blast_config_sharding.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

from urllib.parse import urlparse


def absolute_blob_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if raw.startswith("az://"):
        return "https://" + raw.removeprefix("az://")
    if raw.startswith(("http://", "https://")):
        return raw
    return None


def validate_storage_blob_reference(
    *,
    storage_account: str,
    value: object,
    label: str,
    expected_container: str | None = None,
    require_blob_path: bool = True,
) -> str | None:
    """Validate an absolute Azure Blob URL against the selected Storage account.

    Relative values return ``None`` so callers can continue normalising them into
    the selected account. Absolute values return their canonical ``https://`` URL.
    """

    url = absolute_blob_url(value)
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"{label} URL must use https")
    expected_host = f"{storage_account}.blob.core.windows.net"
    if (parsed.hostname or "").lower() != expected_host.lower():
        raise ValueError(f"{label} URL must belong to the selected Storage account")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} URL must not include query strings or fragments")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError(f"{label} URL must be a plain Azure Blob URL")
    path_parts = parsed.path.lstrip("/").split("/", 1)
    if not path_parts or not path_parts[0]:
        raise ValueError(f"{label} URL must include a container")
    if expected_container and path_parts[0] != expected_container:
        raise ValueError(f"{label} URL must point to the {expected_container} container")
    blob_path = path_parts[1] if len(path_parts) == 2 else ""
    if require_blob_path and not blob_path:
        raise ValueError(f"{label} URL must include a blob path")
    if any(part == ".." for part in blob_path.split("/")):
        raise ValueError(f"{label} URL must not contain '..'")
    return url
