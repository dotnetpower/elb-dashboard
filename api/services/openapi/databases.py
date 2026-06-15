"""Cluster-independent BLAST database catalogue projection for the control plane.

Responsibility: Project the dashboard's own Storage-backed database catalogue
into the ``elb-openapi`` ``/v1/databases`` + ``/v1/databases/{db_name}`` response
shapes so the always-on api sidecar can answer those reads while the AKS cluster
(and therefore the in-cluster ``elb-openapi`` service) is stopped.
Edit boundaries: Pure data-source reuse + response projection only. The blob
enumeration lives in ``storage.database_catalog_cache.list_databases_cached``;
do not re-implement the Azure Blob REST listing here, and do not import
``azure.mgmt.*``. Routes (``api.routes.aks.openapi_databases``) own HTTP/auth.
Key entry points: ``list_databases``, ``get_database``.
Risky contracts: The detail projection mirrors the ``elb-openapi``
``DatabaseMetadata`` field set (``molecule_type`` in ``{dna, protein}`` plus
``molecule_label``, ``snapshot``, ``cached_at``, byte/sequence counts) so an
external caller can swap host without reshaping its parsing.
Validation: ``uv run pytest -q api/tests/test_aks_openapi_databases.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

DEFAULT_CONTAINER = "blast-db"

# Raw molecule token -> (v1 ``molecule_type``, ``molecule_label``). Mirrors the
# ``elb-openapi`` DatabaseMetadata contract: ``molecule_type`` is the lowercase
# natural value (``dna`` / ``protein``) and ``molecule_label`` the display label.
_MOLECULE_MAP: dict[str, tuple[str, str]] = {
    "nucl": ("dna", "mixed DNA"),
    "nucleotide": ("dna", "mixed DNA"),
    "nucleotides": ("dna", "mixed DNA"),
    "dna": ("dna", "mixed DNA"),
    "mixed dna": ("dna", "mixed DNA"),
    "prot": ("protein", "protein"),
    "protein": ("protein", "protein"),
    "proteins": ("protein", "protein"),
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_molecule(raw: str | None) -> tuple[str | None, str]:
    """Map a raw molecule token to ``(molecule_type, molecule_label)``.

    Unknown / empty tokens degrade honestly: an empty token yields
    ``(None, "")`` and an unrecognised token passes through unchanged rather
    than being silently coerced to ``protein``.
    """
    if not raw or not raw.strip():
        return None, ""
    key = raw.strip().casefold()
    mapped = _MOLECULE_MAP.get(key)
    if mapped:
        return mapped
    return raw.strip(), raw.strip()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if cleaned.isdigit() and int(cleaned) > 0:
            return int(cleaned)
    return None


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def list_databases(
    credential: TokenCredential,
    account_name: str,
    container: str = DEFAULT_CONTAINER,
) -> dict[str, Any]:
    """Return the ``elb-openapi`` ``/v1/databases`` list shape from Storage.

    Reuses the dashboard's shared catalogue cache so this control-plane read
    never re-pays the heavy ``blast-db`` enumeration when the SPA already
    warmed it. Enumeration failures propagate to the caller (the route
    classifies them into a degraded payload).
    """
    from api.services.storage.database_catalog_cache import list_databases_cached

    entries = list_databases_cached(credential, account_name, container)
    names = sorted(
        {
            str(entry.get("name") or "").strip()
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("name") or "").strip()
        }
    )
    return {
        "databases": [{"name": name} for name in names],
        "count": len(names),
        "container": container,
    }


def get_database(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    container: str = DEFAULT_CONTAINER,
) -> dict[str, Any] | None:
    """Return the ``elb-openapi`` ``DatabaseMetadata`` shape for one database.

    Returns ``None`` when the named database is not present in the catalogue
    (the route maps that to HTTP 404). Storage failures propagate so the route
    can distinguish a transient outage from a genuine miss.
    """
    from api.services.storage.database_catalog_cache import list_databases_cached

    entries = list_databases_cached(credential, account_name, container)
    match: dict[str, Any] | None = None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == db_name:
            match = entry
            break
    if match is None:
        return None
    return _project_metadata(match, container)


def _project_metadata(entry: dict[str, Any], container: str) -> dict[str, Any]:
    """Project a catalogue entry into the ``elb-openapi`` DatabaseMetadata shape."""
    raw_dbtype = str(entry.get("molecule_type") or "").strip()
    molecule_type, molecule_label = _resolve_molecule(raw_dbtype or None)
    return {
        "name": str(entry.get("name") or "").strip(),
        "container": container,
        "title": str(entry.get("title") or "").strip(),
        "dbtype": raw_dbtype,
        "molecule_type": molecule_type,
        "molecule_label": molecule_label,
        "snapshot": str(entry.get("source_version") or "").strip(),
        "last_updated": _str_or_none(entry.get("update_date"))
        or _str_or_none(entry.get("downloaded_at")),
        "number_of_sequences": _int_or_none(
            entry.get("total_sequences") if entry.get("total_sequences") is not None
            else entry.get("number_of_sequences")
        ),
        "number_of_letters": _int_or_none(
            entry.get("total_letters") if entry.get("total_letters") is not None
            else entry.get("number_of_letters")
        ),
        "number_of_volumes": _int_or_none(entry.get("number_of_volumes")),
        "bytes_total": _int_or_none(entry.get("bytes_total")),
        "bytes_to_cache": _int_or_none(entry.get("bytes_to_cache")),
        "metadata_schema_version": str(entry.get("metadata_schema_version") or "").strip(),
        "cached_at": _utc_now_iso(),
    }
