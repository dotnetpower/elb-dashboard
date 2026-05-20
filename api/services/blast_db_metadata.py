"""BLAST database metadata helpers shared by submit, oracle, and result views.

Responsibility: BLAST database metadata helpers shared by submit, oracle, and result views
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `extract_db_name`, `resolve_db_metadata`, `resolve_database_display_metadata`,
`resolve_blastdb_json_metadata`, `database_display_metadata_from_info`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)


NCBI_DATABASE_CATALOG: dict[str, dict[str, str]] = {
    "core_nt": {
        "title": "Core nucleotide BLAST database",
        "description": (
            "The core nucleotide BLAST database consists of GenBank+EMBL+DDBJ+PDB+RefSeq "
            "sequences, but excludes EST, STS, GSS, WGS, TSA, patent sequences as well as "
            "phase 0, 1, and 2 HTGS sequences and most eukaryotic chromosome sequences. "
            "The database is non-redundant. Identical sequences have been merged into one "
            "entry, while preserving the accession, GI, title and taxonomy information for "
            "each entry."
        ),
        "molecule_type": "mixed DNA",
    }
}


def extract_db_name(database: str) -> str:
    """Extract the bare DB name from a BLAST database value of any supported shape."""
    db = database.strip()
    if not db:
        return ""
    if db.startswith("https://"):
        parsed = urlparse(db)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2 or path_parts[0] != "blast-db":
            return ""
        return path_parts[1]
    db = db.removeprefix("blast-db/")
    return db.split("/", 1)[0]


def resolve_db_metadata(storage_account: str, db_name: str) -> dict[str, Any] | None:
    """Read ``{db}-metadata.json`` from the workload Storage account.

    Returns a parsed dict when present. Missing metadata or transient Storage
    failures return ``None`` so submits can proceed without auto-sharding.
    """
    if not storage_account or not db_name:
        return None
    try:
        from azure.core.exceptions import ResourceNotFoundError

        from api.services import get_credential
        from api.services.storage_data import _blob_service

        service = _blob_service(get_credential(), storage_account)
        container = service.get_container_client("blast-db")
        try:
            data = container.get_blob_client(f"{db_name}-metadata.json").download_blob().readall()
        except ResourceNotFoundError:
            return None
        metadata = json.loads(data.decode("utf-8"))
        if isinstance(metadata, dict):
            return metadata
    except Exception as exc:
        LOGGER.info("db metadata lookup skipped for %s: %s", db_name, type(exc).__name__)
    return None


def resolve_database_display_metadata(
    storage_account: str,
    database: str,
) -> dict[str, Any] | None:
    """Return NCBI-style display metadata for a database used by a job.

    The result page should not need to know where BLAST DB provenance came
    from. We merge the workload Storage catalogue (dynamic counts and snapshot
    date) with a small built-in catalogue for stable NCBI descriptions such as
    ``core_nt``.
    """
    db_name = extract_db_name(database)
    if not db_name:
        return None

    info: dict[str, Any] = {}
    if storage_account:
        blastdb_json = resolve_blastdb_json_metadata(storage_account, db_name) or {}
        storage_metadata = resolve_db_metadata(storage_account, db_name) or {}
        info = {**blastdb_json, **storage_metadata}

    metadata = database_display_metadata_from_info(db_name, info, fallback_database=database)
    return metadata or None


def resolve_blastdb_json_metadata(storage_account: str, db_name: str) -> dict[str, Any] | None:
    """Read the BLAST v5 ``.njs`` metadata blob for one database.

    This avoids listing the entire ``blast-db`` container on job detail reads.
    Older deployments used a flat layout, while the current prepare-db flow
    stores files under ``{db_name}/``; try both shapes plus custom DB layout.
    """
    if not storage_account or not db_name:
        return None
    try:
        from azure.core.exceptions import ResourceNotFoundError

        from api.services import get_credential
        from api.services.storage_data import _blob_service

        service = _blob_service(get_credential(), storage_account)
        container = service.get_container_client("blast-db")
        for blob_name in (
            f"{db_name}/{db_name}.njs",
            f"{db_name}.njs",
            f"custom_db/{db_name}/{db_name}.njs",
        ):
            try:
                data = container.get_blob_client(blob_name).download_blob().readall()
                payload = json.loads(data.decode("utf-8"))
                if isinstance(payload, dict):
                    return _blastdb_json_info(payload)
            except ResourceNotFoundError:
                continue
        return None
    except Exception as exc:
        LOGGER.info(
            "BLAST DB .njs metadata lookup skipped for %s: %s",
            db_name,
            type(exc).__name__,
        )
        return None


def _blastdb_json_info(payload: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for source, target in (
        ("number-of-letters", "total_letters"),
        ("number-of-sequences", "total_sequences"),
        ("bytes-to-cache", "bytes_to_cache"),
        ("bytes-total", "bytes_total"),
    ):
        value = payload.get(source)
        if isinstance(value, (int, float)) and value > 0:
            info[target] = int(value)
    for source, target in (
        ("title", "title"),
        ("description", "description"),
        ("dbtype", "molecule_type"),
        ("last-updated", "update_date"),
        ("last_updated", "update_date"),
        ("date", "update_date"),
    ):
        value = payload.get(source)
        if isinstance(value, str) and value.strip():
            info[target] = value.strip()
    return info


def database_display_metadata_from_info(
    db_name: str,
    info: dict[str, Any] | None,
    *,
    fallback_database: str = "",
) -> dict[str, Any]:
    """Build the result-page database metadata contract from catalogue data."""
    source = info or {}
    catalogue = NCBI_DATABASE_CATALOG.get(db_name, {})
    title = _first_string(source, "title", "db_title", "database_title") or catalogue.get("title")
    description = _description_for_display(source, catalogue, title)
    molecule_type = _normalise_molecule_type(
        _first_string(source, "molecule_type", "dbtype", "db_type")
        or catalogue.get("molecule_type")
    )
    source_version = _first_string(source, "source_version")
    downloaded_at = _first_string(source, "downloaded_at")
    update_date = _normalise_date(
        _first_string(source, "update_date", "last_updated", "last-updated")
        or source_version
        or downloaded_at
    )
    number_of_sequences = _first_positive_int(
        source,
        "number_of_sequences",
        "number-of-sequences",
        "total_sequences",
    )
    number_of_letters = _first_positive_int(
        source,
        "number_of_letters",
        "number-of-letters",
        "total_letters",
    )

    out: dict[str, Any] = {
        "name": db_name,
        "database": fallback_database or db_name,
    }
    optional: dict[str, Any] = {
        "title": title,
        "description": description,
        "molecule_type": molecule_type,
        "update_date": update_date,
        "number_of_sequences": number_of_sequences,
        "number_of_letters": number_of_letters,
        "source_version": source_version,
        "downloaded_at": downloaded_at,
        "source": _first_string(source, "source"),
    }
    for key, value in optional.items():
        if value not in (None, ""):
            out[key] = value
    return out


def _first_string(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _first_positive_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            if cleaned.isdigit() and int(cleaned) > 0:
                return int(cleaned)
    return None


def _normalise_molecule_type(value: str | None) -> str | None:
    if not value:
        return None
    normalised = value.strip()
    lowered = normalised.casefold()
    if lowered in {"nucl", "nucleotide", "nucleotides", "dna"}:
        return "mixed DNA"
    if lowered in {"prot", "protein", "proteins"}:
        return "protein"
    return normalised


def _description_for_display(
    source: dict[str, Any],
    catalogue: dict[str, str],
    title: str | None,
) -> str | None:
    source_description = _first_string(source, "description", "db_description")
    catalogue_description = catalogue.get("description")
    if not source_description:
        return catalogue_description
    if catalogue_description and (
        source_description == title or len(source_description) < len(catalogue_description)
    ):
        return catalogue_description
    return source_description


def _normalise_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    match = re.search(r"(20\d{2})[-/](\d{2})[-/](\d{2})", text)
    if match:
        return f"{match.group(1)}/{match.group(2)}/{match.group(3)}"
    return text
