"""BLAST database discovery from the `blast-db` Storage container.

Responsibility: Inspect BLAST database blobs, metadata JSON, oracle status, and
    oracle automation control documents, plus BLAST v5 `.njs` files to produce
    the dashboard database catalogue payload.
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
    oracle_status_blobs: dict[str, str] = {}  # db_name -> current-ready json
    oracle_active_blobs: dict[str, str] = {}  # db_name -> active run json
    oracle_automation_blobs: dict[str, str] = {}  # db_name -> retry state json
    oracle_latest_run_names: dict[str, str] = {}  # db_name -> latest run status path
    oracle_latest_run_blobs: dict[str, str] = {}
    oracle_part_names: dict[tuple[str, str], set[str]] = {}
    blastdb_json_blobs: dict[str, str] = {}  # db_name -> BLAST v5 .njs content
    # Per-base name of the .njs blob we will download for display metadata.
    # A multi-volume DB (e.g. core_nt.00.njs … core_nt.99.njs) has one .njs per
    # volume; historically we downloaded EVERY one and kept only the last (the
    # classic N+1 — App Insights showed ~600 BlobClient.download_blob calls for
    # a single /api/blast/databases load). list_blobs() returns names in
    # lexicographic order, so "last wins" == the highest-numbered volume's .njs.
    # We record only the name here and defer the single download to after the
    # enumeration loop, preserving the exact "last wins" content while paying
    # one download per base instead of one per volume.
    blastdb_json_names: dict[str, str] = {}  # db_name -> .njs blob name (last wins)
    generation_info: dict[str, dict[str, Any]] = {}  # active basename prefix -> stats
    generation_njs_names: dict[str, str] = {}  # active basename prefix -> .njs blob
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
            len(parts) == 6
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] == "runs"
            and parts[5] == "status.json"
        ):
            db_name = parts[2]
            previous = oracle_latest_run_names.get(db_name, "")
            if blob.name > previous:
                oracle_latest_run_names[db_name] = blob.name
            continue
        if (
            len(parts) == 4
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] in {"status.json", "active.json", "automation.json"}
        ):
            try:
                bc = cc.get_blob_client(blob.name)
                content = read_metadata_blob_text(
                    bc, max_bytes=4 * 1024 * 1024, label="oracle-status.json"
                )
                if parts[3] == "status.json":
                    oracle_status_blobs[parts[2]] = content
                elif parts[3] == "active.json":
                    oracle_active_blobs[parts[2]] = content
                else:
                    oracle_automation_blobs[parts[2]] = content
            except Exception as exc:
                LOGGER.debug("oracle status blob read skipped for %s: %s", blob.name, exc)
            continue
        if (
            len(parts) == 6
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] == "parts"
            and name.endswith(".txt")
        ):
            oracle_part_key = (parts[2], parts[4])
            oracle_part_names.setdefault(oracle_part_key, set()).add(blob.name)
            continue
        # Generation-scoped files are invisible until metadata.active_prefix
        # selects their exact basename prefix. Keeping them out of the legacy
        # aggregation prevents an in-progress Direct download from inflating
        # file_count/bytes or replacing the displayed .njs before promotion.
        if len(parts) >= 4 and parts[1] == "generations":
            for ext in _DB_EXTS:
                if not name.endswith(ext):
                    continue
                base = re.sub(r"\.\d+$", "", name[: -len(ext)])
                if not base:
                    break
                directory = "/".join(parts[:-1])
                db_prefix = f"{directory}/{base}"
                info = generation_info.setdefault(
                    db_prefix,
                    {
                        "name": base,
                        "container": container,
                        "prefix": directory,
                        "source": "ncbi",
                        "file_count": 0,
                        "total_bytes": 0,
                        "last_modified": None,
                    },
                )
                info["file_count"] += 1
                info["total_bytes"] += blob.size or 0
                if blob.last_modified:
                    modified = (
                        blob.last_modified.isoformat()
                        if hasattr(blob.last_modified, "isoformat")
                        else str(blob.last_modified)
                    )
                    if not info["last_modified"] or modified > info["last_modified"]:
                        info["last_modified"] = modified
                if ext == ".njs":
                    generation_njs_names[db_prefix] = blob.name
                break
            continue
        if name.endswith(".njs"):
            base = re.sub(r"\.\d+$", "", name[:-4])
            # Defer the download: record the name (last lexicographic volume
            # wins, matching the previous behaviour). The actual read happens
            # once per base after the loop.
            blastdb_json_names[base] = blob.name
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
                        # Build the blob prefix so the frontend can reconstruct
                        # the full path. The prefix is the *directory* the DB
                        # files actually live in, NOT the filename base. Using
                        # the base broke nested subset DBs such as
                        # ``nt/nt_euk.*`` (folder ``nt`` != base ``nt_euk``):
                        # the old prefix ``nt_euk`` produced the path
                        # ``blast-db/nt_euk/nt_euk`` which does not exist, so
                        # the submit pre-flight reported the DB as missing even
                        # though the dashboard listed it as "Downloaded".
                        # ``parts[:-1]`` is the real directory (empty for a
                        # top-level file, ``custom_db/<db>`` for custom builds).
                        prefix = "/".join(parts[:-1])
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
    # Select a staged generation only after its metadata pointer is active.
    # Legacy databases without active_prefix retain the original aggregation.
    import json as _json

    for db_name, raw_metadata in metadata_blobs.items():
        try:
            metadata = _json.loads(raw_metadata)
        except (TypeError, _json.JSONDecodeError) as exc:
            LOGGER.debug("active generation metadata parse skipped for %s: %s", db_name, exc)
            continue
        active_prefix = str(metadata.get("active_prefix") or "").strip("/")
        active_info = generation_info.get(active_prefix)
        if active_info is None:
            continue
        db_info[db_name] = dict(active_info)
        active_njs = generation_njs_names.get(active_prefix)
        if active_njs:
            blastdb_json_names[db_name] = active_njs
    # Deferred single .njs download per base (see blastdb_json_names above).
    # Only read the .njs for bases that actually registered as a database;
    # the enrichment loop below reads blastdb_json_blobs[db_name] only for
    # db_name in db_info, so a .njs whose base was filtered out (staging,
    # shards, …) would be a wasted round-trip.
    for base, njs_name in blastdb_json_names.items():
        if base not in db_info:
            continue
        try:
            bc = cc.get_blob_client(njs_name)
            blastdb_json_blobs[base] = read_metadata_blob_text(bc, label="blast-db-njs")
        except Exception as exc:
            LOGGER.debug("BLAST DB metadata read skipped for %s: %s", njs_name, exc)
    for base, status_name in oracle_latest_run_names.items():
        if base not in db_info:
            continue
        try:
            oracle_latest_run_blobs[base] = read_metadata_blob_text(
                cc.get_blob_client(status_name),
                max_bytes=4 * 1024 * 1024,
                label="oracle-run-status.json",
            )
        except Exception as exc:
            LOGGER.debug("oracle run status read skipped for %s: %s", status_name, exc)
    # Enrich with metadata (source_version, downloaded_at, sharding info)
    from api.services.web_blast_searchsp import (
        WEB_BLAST_SEARCHSP_DEFAULTS,
        compute_web_blast_searchsp,
    )

    for db_name, info in db_info.items():
        # Default sharding fields so the frontend can rely on their presence.
        info.setdefault("sharded", False)
        info.setdefault("shard_sets", [])
        info.setdefault("shard_source_version", None)
        info.setdefault("shard_layout_schema", 0)
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
                info["active_prefix"] = meta.get("active_prefix")
                info["active_generation"] = meta.get("active_generation")
                info["pending_generation"] = meta.get("pending_generation")
                info["source_provider"] = meta.get("source_provider")
                info["source_release_at"] = meta.get("source_release_at")
                info["release_fingerprint"] = meta.get("release_fingerprint")
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
                shard_layout_schema = meta.get("shard_layout_schema")
                if isinstance(shard_layout_schema, int) and shard_layout_schema >= 0:
                    info["shard_layout_schema"] = shard_layout_schema
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
                        item for item in meta["failed_files"] if isinstance(item, dict)
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
        oracle_payload: dict[str, Any] | None = None
        if db_name in oracle_status_blobs:
            try:
                oracle = _json.loads(oracle_status_blobs[db_name])
                if isinstance(oracle, dict):
                    run_id = str(oracle.get("run_id") or "")
                    expected_parts = int(oracle.get("expected_parts") or 0)
                    names = oracle_part_names.get((db_name, run_id), set())
                    expected_shards = [
                        shard
                        for shard in oracle.get("expected_shards", [])
                        if isinstance(shard, str)
                    ]
                    ready_parts = (
                        len(
                            names
                            & {
                                f"metadata/oracles/{db_name}/parts/{run_id}/{shard}.txt"
                                for shard in expected_shards
                            }
                        )
                        if expected_shards
                        else len(names)
                    )
                    db_source_version = str(info.get("source_version") or "")
                    oracle_source_version = str(oracle.get("source_version") or "")
                    source_version_stale = bool(
                        db_source_version and oracle_source_version != db_source_version
                    )
                    oracle_payload = {
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
        if db_name in oracle_active_blobs:
            try:
                active = _json.loads(oracle_active_blobs[db_name])
                if isinstance(active, dict):
                    active_run_id = str(active.get("run_id") or "")
                    active_names = oracle_part_names.get((db_name, active_run_id), set())
                    active_shards = [
                        shard
                        for shard in active.get("expected_shards", [])
                        if isinstance(shard, str)
                    ]
                    active_payload = {
                        "status": str(active.get("status") or "building"),
                        "phase": active.get("phase"),
                        "run_id": active.get("run_id"),
                        "started_at": active.get("started_at"),
                        "source_version": active.get("source_version"),
                        "expected_parts": int(active.get("expected_parts") or 0),
                        "ready_parts": (
                            len(
                                active_names
                                & {
                                    f"metadata/oracles/{db_name}/parts/{active_run_id}/{shard}.txt"
                                    for shard in active_shards
                                }
                            )
                            if active_shards
                            else len(active_names)
                        ),
                        "automatic": bool(active.get("automatic")),
                    }
                    if oracle_payload is None:
                        oracle_payload = dict(active_payload)
                    oracle_payload["active"] = active_payload
            except Exception as exc:
                LOGGER.debug("oracle active blob parse skipped for %s: %s", db_name, exc)
        if db_name in oracle_automation_blobs:
            try:
                automation = _json.loads(oracle_automation_blobs[db_name])
                if isinstance(automation, dict):
                    automation_payload = {
                        key: automation.get(key)
                        for key in (
                            "status",
                            "failure_count",
                            "retry_exhausted",
                            "next_retry_at",
                            "last_run_id",
                            "last_error_code",
                            "blocked_reason",
                            "updated_at",
                        )
                    }
                    if oracle_payload is None:
                        oracle_payload = {
                            "status": str(automation.get("status") or "idle"),
                            "expected_parts": 0,
                            "ready_parts": 0,
                        }
                    oracle_payload["automation"] = automation_payload
            except Exception as exc:
                LOGGER.debug("oracle automation blob parse skipped for %s: %s", db_name, exc)
        if db_name in oracle_latest_run_blobs:
            try:
                latest_attempt = _json.loads(oracle_latest_run_blobs[db_name])
                latest_status = str(latest_attempt.get("status") or "")
                latest_run_id = str(latest_attempt.get("run_id") or "")
                current_run_id = str((oracle_payload or {}).get("run_id") or "")
                if (
                    isinstance(latest_attempt, dict)
                    and latest_status in {"failed", "superseded", "timeout"}
                    and latest_run_id
                    and latest_run_id != current_run_id
                ):
                    last_attempt = {
                        "status": latest_status,
                        "phase": latest_attempt.get("phase"),
                        "run_id": latest_run_id,
                        "error_code": latest_attempt.get("error_code"),
                        "finished_at": latest_attempt.get("finished_at"),
                        "automatic": bool(latest_attempt.get("automatic")),
                    }
                    if oracle_payload is None:
                        oracle_payload = {
                            "status": latest_status,
                            "run_id": latest_run_id,
                            "expected_parts": int(latest_attempt.get("expected_parts") or 0),
                            "ready_parts": int(latest_attempt.get("ready_parts") or 0),
                        }
                    oracle_payload["last_attempt"] = last_attempt
            except Exception as exc:
                LOGGER.debug("latest oracle run parse skipped for %s: %s", db_name, exc)
        if oracle_payload is not None:
            info["db_order_oracle"] = oracle_payload
        default_searchsp = WEB_BLAST_SEARCHSP_DEFAULTS.get(db_name)
        if default_searchsp is not None:
            # Recompute the verified Web BLAST search space from the LIVE
            # snapshot statistics so it auto-adapts to drift (a re-downloaded
            # core_nt with slightly different db-len/db-num). The submit gate
            # and compatibility contract recompute the same value from the
            # forwarded db_total_letters/db_total_sequences, so a precise run
            # stays Web BLAST-compatible without a manual recalibration. Falls
            # back to the pinned value when the live stats are unavailable.
            live_len = info.get("total_letters")
            live_num = info.get("total_sequences")
            recomputed = (
                compute_web_blast_searchsp(int(live_len), int(live_num))
                if isinstance(live_len, int) and isinstance(live_num, int)
                else None
            )
            if recomputed is not None:
                info.setdefault("web_blast_searchsp", recomputed)
                info.setdefault("web_blast_searchsp_source", "recomputed_live_snapshot")
            else:
                info.setdefault("web_blast_searchsp", default_searchsp.value)
                info.setdefault("web_blast_searchsp_source", "pinned_calibration")
            info.setdefault("web_blast_searchsp_scope", default_searchsp.scope)
            info.setdefault("web_blast_searchsp_evidence", default_searchsp.evidence)
        # Derived readiness fields — let SPA / preflight read one boolean
        # instead of re-deriving "copy_status.phase == 'completed'" four
        # different ways. Keep the upstream `copy_status` / `update_in_progress`
        # fields untouched so any consumer that wants the raw lifecycle still
        # gets it.
        _ready, _not_ready_reason = _derive_db_readiness(info)
        info["ready"] = _ready
        info["not_ready_reason"] = _not_ready_reason
    return sorted(db_info.values(), key=lambda d: d["name"])


def _derive_db_readiness(info: dict[str, Any]) -> tuple[bool, str | None]:
    """Mirror the SPA's `getBlastDbReadiness` so a single contract lives on the
    server. Returns ``(ready, reason)`` where ``reason`` is one of
    ``copying`` / ``partial`` / ``init_failed`` / ``cancelled`` /
    ``unknown_phase`` / ``updating`` / ``empty`` / ``None``.
    """
    copy_status = info.get("copy_status")
    if isinstance(copy_status, dict):
        phase = str(copy_status.get("phase") or "")
        if phase:
            if phase == "completed":
                return True, None
            if phase in {"copying", "partial", "init_failed", "cancelled"}:
                return False, phase
            return False, "unknown_phase"
    if info.get("update_in_progress"):
        return False, "updating"
    file_count = info.get("file_count")
    if isinstance(file_count, (int, float)) and file_count > 0:
        return True, None
    return False, "empty"
