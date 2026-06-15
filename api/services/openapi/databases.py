"""Cluster-independent BLAST database catalogue projection for the control plane.

Responsibility: Project the dashboard's own Storage-backed database catalogue
into the ``elb-openapi`` ``/v1/databases`` + ``/v1/databases/{db_name}`` response
shapes so the always-on api sidecar can answer those reads while the AKS cluster
(and therefore the in-cluster ``elb-openapi`` service) is stopped.
Edit boundaries: Pure data-source reuse + response projection only. The catalogue
listing reuses ``storage.database_catalog_cache.list_databases_cached``; the
single-database metadata reads the SAME NCBI metadata blobs as ``elb-openapi``
(``{db}/{db}-nucl-metadata.json`` / ``-prot-metadata.json``) so the detail
response is a true drop-in. Do not re-implement the Azure Blob REST listing here,
and do not import ``azure.mgmt.*``. Routes (``api.routes.aks.openapi_databases``)
own HTTP/auth.
Key entry points: ``list_databases``, ``get_database``.
Risky contracts: The detail projection mirrors the ``elb-openapi``
``DatabaseMetadata`` field set (``molecule_type`` in ``{dna, protein}`` decided by
WHICH suffix blob exists — not from the catalogue's coarse ``.njs`` enrichment,
which leaves single-volume DBs like 16S/18S/ITS with ``molecule_type=null`` — plus
``molecule_label``, ``snapshot`` from the ``files[]`` path regex, ``cached_at``,
byte/sequence counts) so an external caller can swap host without reshaping its
parsing.
Validation: ``uv run pytest -q api/tests/test_aks_openapi_databases.py``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from azure.core.credentials import TokenCredential

LOGGER = logging.getLogger(__name__)

DEFAULT_CONTAINER = "blast-db"

# NCBI snapshot stamp embedded in the metadata ``files[]`` blob paths, e.g.
# ``.../2026-05-26-01-05-01/...``. Mirrors elb-openapi's ``_SNAPSHOT_RE`` so the
# ``snapshot`` field matches byte-for-byte.
_SNAPSHOT_RE = re.compile(r"/(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})/")

# (raw suffix-derived molecule token) -> (v1 ``molecule_type``, ``molecule_label``).
# In the metadata path the molecule is decided by WHICH suffix blob exists
# (``-nucl-metadata.json`` => nucleotide, ``-prot-metadata.json`` => protein),
# exactly like elb-openapi, so only the two canonical tokens occur here.
_MOLECULE_MAP: dict[str, tuple[str, str]] = {
    "nucl": ("dna", "mixed DNA"),
    "prot": ("protein", "protein"),
}

# Candidate (molecule token, blob suffix) pairs, tried in order. Nucleotide
# first because the prepared catalogue is overwhelmingly nucleotide.
_METADATA_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("nucl", "-nucl-metadata.json"),
    ("prot", "-prot-metadata.json"),
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_molecule(token: str) -> tuple[str, str]:
    """Map a suffix-derived molecule token to ``(molecule_type, molecule_label)``."""
    return _MOLECULE_MAP[token]


def _raw_int(value: Any) -> int | None:
    """Pass through a positive integer count, else ``None``.

    NCBI metadata stores ``number-of-*`` as plain ints; a bool or non-int is
    treated as absent so the response stays ``Optional[int]``-clean.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _title_from_raw(raw: dict[str, Any]) -> str:
    """First non-empty of description/display_name/title/name (elb-openapi order)."""
    for key in ("description", "display_name", "title", "name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _snapshot_from_raw(raw: dict[str, Any]) -> str:
    """Extract the NCBI snapshot stamp from ``files[]``; ``"unknown"`` if absent."""
    files = raw.get("files") if isinstance(raw.get("files"), list) else []
    for item in files:
        match = _SNAPSHOT_RE.search(str(item))
        if match:
            return match.group(1)
    return "unknown"


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

    Reads the SAME NCBI metadata blobs as ``elb-openapi`` —
    ``{db}/{db}-nucl-metadata.json`` then ``{db}/{db}-prot-metadata.json`` — and
    decides ``molecule_type`` by which suffix exists. This is deliberately NOT
    sourced from ``list_databases_cached``: that catalogue's ``.njs`` enrichment
    leaves single-volume DBs (16S/18S/ITS) with ``molecule_type``/counts/title
    null, so reusing it would break the "drop-in" contract.

    Returns ``None`` only when BOTH suffix blobs are genuinely absent (the route
    maps that to HTTP 404). A transient Storage failure on any candidate is
    re-raised so the route returns 503 (never a silent miss).
    """
    from azure.core.exceptions import ResourceNotFoundError

    from api.services.storage.blob_io import read_metadata_blob_bytes
    from api.services.storage.data import _blob_service

    service = _blob_service(credential, account_name)
    cc = service.get_container_client(container)

    last_exc: Exception | None = None
    for molecule_token, suffix in _METADATA_CANDIDATES:
        blob_name = f"{db_name}/{db_name}{suffix}"
        try:
            data = read_metadata_blob_bytes(
                cc.get_blob_client(blob_name), label="db-ncbi-metadata"
            )
        except ResourceNotFoundError:
            # This suffix does not exist — try the other molecule.
            continue
        except Exception as exc:
            last_exc = exc
            continue
        if not data:
            # 0-byte blob: treat as a transient/corrupt read, not a miss.
            last_exc = ValueError(f"empty metadata blob for {blob_name}")
            continue
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError as exc:
            last_exc = exc
            continue
        if isinstance(payload, dict):
            return _project_metadata(db_name, payload, molecule_token, container)
        last_exc = ValueError(f"non-object metadata JSON for {blob_name}")

    if last_exc is not None:
        # A transient/corrupt read outweighs a sibling 404 — we do not know
        # whether the failed suffix was the right one, so surface it as 503
        # rather than synthesise a false "not found".
        raise last_exc
    return None


def _project_metadata(
    db_name: str,
    raw: dict[str, Any],
    molecule_token: str,
    container: str,
) -> dict[str, Any]:
    """Project raw NCBI metadata JSON into the ``elb-openapi`` DatabaseMetadata shape."""
    molecule_type, molecule_label = _resolve_molecule(molecule_token)
    return {
        "name": db_name,
        "container": container,
        "title": _title_from_raw(raw),
        "dbtype": str(raw.get("dbtype") or "").strip(),
        "molecule_type": molecule_type,
        "molecule_label": molecule_label,
        "snapshot": _snapshot_from_raw(raw),
        "last_updated": _str_or_none(raw.get("last-updated"))
        or _str_or_none(raw.get("last_updated"))
        or _str_or_none(raw.get("date")),
        "number_of_sequences": _raw_int(raw.get("number-of-sequences")),
        "number_of_letters": _raw_int(raw.get("number-of-letters")),
        "number_of_volumes": _raw_int(raw.get("number-of-volumes")),
        "bytes_total": _raw_int(raw.get("bytes-total")),
        "bytes_to_cache": _raw_int(raw.get("bytes-to-cache")),
        "metadata_schema_version": str(raw.get("version") or "").strip(),
        "cached_at": _utc_now_iso(),
    }

