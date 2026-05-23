"""BLAST database discovery from the `blast-db` Storage container.

Responsibility: Inspect BLAST database blobs, metadata JSON, oracle status, and
BLAST v5 `.njs` files to produce the dashboard database catalogue payload.
Edit boundaries: Database catalogue/listing only. Generic blob I/O and Storage
failure classification live in sibling modules.
Key entry points: `list_databases`.
Risky contracts: Metadata reads are capped via `read_metadata_blob_text`; do not
load unbounded blob contents into memory.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.storage.blob_io import read_metadata_blob_text
from api.services.storage.client_pool import _blob_service

LOGGER = logging.getLogger(__name__)


def list_databases(
    credential: TokenCredential,
    account_name: str,
    container: str = "blast-db",
) -> list[dict[str, Any]]:
    """List available BLAST databases in the blast-db container.

    BLAST databases consist of multiple files like core_nt.00.nhd,
    core_nt.00.nhi, core_nt.nal, etc. We extract the base DB name
    by stripping the volume number and extension suffixes.
    """
    # Known BLAST DB file extensions
    _DB_EXTS = {
        ".nhd",
        ".nhi",
        ".nhr",
        ".nin",
        ".nnd",
        ".nni",
        ".nog",
        ".nsq",
        ".nxm",
        ".nal",
        ".ndb",
        ".njs",
        ".nos",
        ".not",
        ".ntf",
        ".nto",
        ".phd",
        ".phi",
        ".phr",
        ".pin",
        ".pnd",
        ".pni",
        ".pog",
        ".psq",
        ".pxm",
        ".pal",
        ".pdb",
        ".pjs",
        ".pos",
        ".pot",
        ".ptf",
        ".pto",
    }
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    db_info: dict[str, dict[str, Any]] = {}
    metadata_blobs: dict[str, str] = {}  # db_name -> metadata json content
    oracle_status_blobs: dict[str, str] = {}  # db_name -> oracle status json content
    oracle_part_counts: dict[str, int] = {}
    blastdb_json_blobs: dict[str, str] = {}  # db_name -> BLAST v5 .njs content
    for blob in cc.list_blobs():
        parts = blob.name.split("/")
        name = parts[-1]  # file name without directory prefix
        # Detect the prefix to distinguish NCBI (top-level) from custom (custom_db/)
        is_custom = len(parts) >= 3 and parts[0] == "custom_db"
        # Collect metadata files separately
        if name.endswith("-metadata.json"):
            meta_db_name = name.replace("-metadata.json", "")
            try:
                bc = cc.get_blob_client(blob.name)
                metadata_blobs[meta_db_name] = read_metadata_blob_text(
                    bc, max_bytes=4 * 1024 * 1024, label="db-metadata.json"
                )
            except Exception as exc:
                LOGGER.debug("metadata blob read skipped for %s: %s", blob.name, exc)
            continue
        if (
            len(parts) == 4
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] == "status.json"
        ):
            try:
                bc = cc.get_blob_client(blob.name)
                oracle_status_blobs[parts[2]] = read_metadata_blob_text(
                    bc, max_bytes=4 * 1024 * 1024, label="oracle-status.json"
                )
            except Exception as exc:
                LOGGER.debug("oracle status blob read skipped for %s: %s", blob.name, exc)
            continue
        if (
            len(parts) == 6
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] == "parts"
        ):
            oracle_part_counts[parts[2]] = oracle_part_counts.get(parts[2], 0) + 1
            continue
        if name.endswith(".njs"):
            base = re.sub(r"\.\d+$", "", name[:-4])
            try:
                bc = cc.get_blob_client(blob.name)
                blastdb_json_blobs[base] = read_metadata_blob_text(bc, label="blast-db-njs")
            except Exception as exc:
                LOGGER.debug("BLAST DB metadata read skipped for %s: %s", blob.name, exc)
        # Skip staging artifacts
        if parts[0] in ("custom-db-build",) or (len(parts) >= 2 and parts[1] == ".staging"):
            continue
        # Skip prepare-db shard layout artifacts. ensure_shard_sets() writes
        # files under `{N}shards/{db}_shard_{i:02d}/...` (manifest + .nal).
        # Without this guard, the .nal at e.g.
        # `1shards/16S_ribosomal_RNA_shard_00/16S_ribosomal_RNA_shard_00.nal`
        # would be parsed as a brand-new "DB" called
        # `16S_ribosomal_RNA_shard_00`, polluting the dashboard.
        if re.match(r"^\d+shards$", parts[0]):
            continue
        # Check if file has a known BLAST extension
        for ext in _DB_EXTS:
            if name.endswith(ext):
                base = name[: -len(ext)]
                # Strip volume number suffix (e.g. ".00", ".01")
                base = re.sub(r"\.\d+$", "", base)
                if base:
                    if base not in db_info:
                        # Build the blob prefix so the frontend can reconstruct the full path
                        prefix = f"custom_db/{base}" if is_custom else base
                        db_info[base] = {
                            "name": base,
                            "container": container,
                            "prefix": prefix,
                            "source": "custom" if is_custom else "ncbi",
                            "file_count": 0,
                            "total_bytes": 0,
                            "last_modified": None,
                        }
                    db_info[base]["file_count"] += 1
                    db_info[base]["total_bytes"] += blob.size or 0
                    blob_modified = blob.last_modified
                    if blob_modified:
                        mod_str = (
                            blob_modified.isoformat()
                            if hasattr(blob_modified, "isoformat")
                            else str(blob_modified)
                        )
                        prev = db_info[base]["last_modified"]
                        if not prev or mod_str > prev:
                            db_info[base]["last_modified"] = mod_str
                break
    # Enrich with metadata (source_version, downloaded_at, sharding info)
    import json as _json

    from api.services.web_blast_searchsp import WEB_BLAST_SEARCHSP_DEFAULTS

    for db_name, info in db_info.items():
        # Default sharding fields so the frontend can rely on their presence.
        info.setdefault("sharded", False)
        info.setdefault("shard_sets", [])
        info.setdefault("shard_source_version", None)
        info.setdefault("shards_stale", False)
        info.setdefault("sharding_in_progress", False)
        info.setdefault("sharding_started_at", None)
        info.setdefault("sharding_error", None)
        info.setdefault("update_in_progress", False)
        info.setdefault("updating_to_source_version", None)
        info.setdefault("update_started_at", None)
        info.setdefault("update_completed_at", None)
        info.setdefault("update_error", None)
        info.setdefault("update_failed_at", None)
        if db_name in blastdb_json_blobs:
            try:
                blast_meta = _json.loads(blastdb_json_blobs[db_name])
                for source, target in (
                    ("number-of-letters", "total_letters"),
                    ("number-of-sequences", "total_sequences"),
                    ("bytes-to-cache", "bytes_to_cache"),
                    ("bytes-total", "bytes_total"),
                ):
                    value = blast_meta.get(source)
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
                    value = blast_meta.get(source)
                    if isinstance(value, str) and value.strip():
                        info[target] = value.strip()
            except Exception as exc:
                LOGGER.debug("BLAST DB .njs metadata parse skipped for %s: %s", db_name, exc)
        if db_name in metadata_blobs:
            try:
                meta = _json.loads(metadata_blobs[db_name])
                info["source_version"] = meta.get("source_version")
                info["downloaded_at"] = meta.get("downloaded_at")
                # Sharding metadata written by the prepare-db pipeline once
                # the per-DB shard set upload completes. Both keys are
                # optional — older metadata blobs (pre-2026-05) won't have
                # them, in which case the defaults above hold.
                if isinstance(meta.get("sharded"), bool):
                    info["sharded"] = meta["sharded"]
                shard_sets = meta.get("shard_sets")
                if isinstance(shard_sets, list):
                    # Coerce to a sorted list of unique ints for a stable
                    # contract with the SPA.
                    info["shard_sets"] = sorted(
                        {
                            int(n)
                            for n in shard_sets
                            if isinstance(n, (int, str)) and str(n).isdigit()
                        }
                    )
                shard_source_version = meta.get("shard_source_version")
                if isinstance(shard_source_version, str) and shard_source_version.strip():
                    info["shard_source_version"] = shard_source_version.strip()
                elif info.get("sharded") and info.get("source_version"):
                    # Legacy metadata predates explicit shard generation tagging; treat
                    # the existing layouts as belonging to the recorded DB generation.
                    info["shard_source_version"] = info.get("source_version")
                db_source_version = str(info.get("source_version") or "")
                shard_version = str(info.get("shard_source_version") or "")
                info["shards_stale"] = bool(
                    info.get("sharded") and db_source_version and shard_version != db_source_version
                )
                # In-flight shard state surfaced from the daemon-thread
                # writer in /api/blast/databases/{db}/shard. The SPA
                # renders these directly so a page reload still shows
                # "sharding…" while a background thread is running.
                if isinstance(meta.get("sharding_in_progress"), bool):
                    info["sharding_in_progress"] = meta["sharding_in_progress"]
                if isinstance(meta.get("sharding_started_at"), str):
                    info["sharding_started_at"] = meta["sharding_started_at"]
                if isinstance(meta.get("sharding_error"), str):
                    info["sharding_error"] = meta["sharding_error"][:300]
                if isinstance(meta.get("update_in_progress"), bool):
                    info["update_in_progress"] = meta["update_in_progress"]
                for key in (
                    "updating_to_source_version",
                    "update_started_at",
                    "update_completed_at",
                    "update_failed_at",
                ):
                    if isinstance(meta.get(key), str):
                        info[key] = meta[key]
                if isinstance(meta.get("update_error"), str):
                    info["update_error"] = meta["update_error"][:300]
                # Hardened prepare-db pipeline fields. ``copy_status`` is the
                # authoritative replacement for the SPA's old "90% of files
                # arrived = Ready" heuristic — when phase == "completed" the
                # download truly succeeded; "partial" / "init_failed" /
                # "copying" are honest in-flight or partial states.
                if isinstance(meta.get("copy_status"), dict):
                    info["copy_status"] = meta["copy_status"]
                if isinstance(meta.get("failed_files"), list):
                    info["failed_files"] = [
                        item
                        for item in meta["failed_files"]
                        if isinstance(item, dict)
                    ][:50]
                # ETag of a stable NCBI key (the .tar.gz.md5 we picked when
                # the DB was prepared). The SPA uses it for per-DB update
                # detection that does NOT fire whenever NCBI rotates
                # latest-dir.
                if isinstance(meta.get("signature_etag"), str):
                    info["signature_etag"] = meta["signature_etag"]
                # Composite signature (sha256-16 hex of N sampled md5 ETags)
                # — preferred over signature_etag for multi-volume DBs. The
                # check-updates route picks composite > etag > snapshot.
                if isinstance(meta.get("composite_signature"), str):
                    info["composite_signature"] = meta["composite_signature"]
                # Allow metadata to override total_bytes if the prepare-db
                # pipeline computed it more precisely than blob enumeration
                # (e.g. for very large multi-volume DBs).
                if isinstance(meta.get("total_bytes"), (int, float)) and meta["total_bytes"] > 0:
                    info["total_bytes"] = int(meta["total_bytes"])
                for key in ("total_letters", "total_sequences", "bytes_to_cache", "bytes_total"):
                    if isinstance(meta.get(key), (int, float)) and meta[key] > 0:
                        info[key] = int(meta[key])
                for source_key in ("effective_search_space", "db_effective_search_space"):
                    if isinstance(meta.get(source_key), (int, float)) and meta[source_key] > 0:
                        info["db_effective_search_space"] = int(meta[source_key])
                        info["db_effective_search_space_source"] = "storage_metadata"
                        break
            except Exception as exc:
                LOGGER.debug("metadata blob parse skipped for %s: %s", db_name, exc)
        if db_name in oracle_status_blobs:
            try:
                oracle = _json.loads(oracle_status_blobs[db_name])
                if isinstance(oracle, dict):
                    expected_parts = int(oracle.get("expected_parts") or 0)
                    ready_parts = int(oracle_part_counts.get(db_name, 0))
                    db_source_version = str(info.get("source_version") or "")
                    oracle_source_version = str(oracle.get("source_version") or "")
                    source_version_stale = bool(
                        db_source_version and oracle_source_version != db_source_version
                    )
                    info["db_order_oracle"] = {
                        "status": (
                            "stale"
                            if source_version_stale
                            else "ready"
                            if expected_parts > 0 and ready_parts >= expected_parts
                            else str(oracle.get("status") or "building")
                        ),
                        "run_id": oracle.get("run_id"),
                        "started_at": oracle.get("started_at"),
                        "source_version": oracle.get("source_version"),
                        "expected_parts": expected_parts,
                        "ready_parts": ready_parts,
                        "part_prefix": oracle.get("part_prefix"),
                    }
            except Exception as exc:
                LOGGER.debug("oracle status blob parse skipped for %s: %s", db_name, exc)
        default_searchsp = WEB_BLAST_SEARCHSP_DEFAULTS.get(db_name)
        if default_searchsp is not None:
            info.setdefault("web_blast_searchsp", default_searchsp.value)
            info.setdefault("web_blast_searchsp_scope", default_searchsp.scope)
            info.setdefault("web_blast_searchsp_evidence", default_searchsp.evidence)
    return sorted(db_info.values(), key=lambda d: d["name"])
