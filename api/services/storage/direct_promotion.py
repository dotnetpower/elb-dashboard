"""Validate and atomically promote staged NCBI Direct database generations.

Responsibility: Verify immutable Direct archive markers and staged blobs, publish
    generation-scoped shard layouts, and atomically switch the active generation.
Edit boundaries: Storage validation and promotion only; NCBI discovery, Kubernetes
    download dispatch/polling, Celery state, and orphan scanning stay in their owners.
Key entry points: `verify_direct_generation`, `promote_direct_generation`,
    `recover_direct_generation`.
Risky contracts: Every checkpoint revalidates `prepare_operation_id`; promotion requires
    the persisted generation identity, transfer hash, archive count, exact blob sizes, and
    complete shard layouts. Recovery may reacquire only the same global lock owner after
    ephemeral Redis loss and never changes the active generation on partial failure.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_task.py
    api/tests/test_orphan_prepare_db_reconcile.py api/tests/test_ncbi_direct_lock.py`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from api.services.db.generations import generation_db_prefix
from api.services.db.sharding import ensure_shard_sets, require_complete_shard_summary
from api.services.storage.prepare_db_metadata import (
    DatabaseOperationOwnershipError,
    download_blob_with_etag,
    require_prepare_operation_owner,
    update_metadata,
)

LOGGER = logging.getLogger(__name__)


class DirectGenerationIncompleteError(RuntimeError):
    """The persisted staged generation cannot safely be promoted."""


class DirectRecoveryBusyError(RuntimeError):
    """Another owner currently holds the deployment-wide Direct transfer lock."""


def verify_direct_generation(
    container: Any,
    *,
    data_prefix: str,
    archive_count: int,
    transfer_sha: str,
) -> tuple[list[str], int]:
    """Return staged files and bytes only when every immutable marker matches."""
    from api.services.storage.blob_io import read_metadata_blob_text

    if archive_count < 1:
        raise DirectGenerationIncompleteError("Direct generation archive count is invalid")
    expected_markers = {
        f"{data_prefix}/.manifests/{index:02d}.json" for index in range(archive_count)
    }
    present: dict[str, Any] = {}
    blob_sizes: dict[str, int] = {}
    for blob in container.list_blobs(name_starts_with=f"{data_prefix}/"):
        name = str(getattr(blob, "name", "") or "")
        blob_sizes[name] = int(getattr(blob, "size", 0) or 0)
        if name in expected_markers:
            present[name] = blob
    if set(present) != expected_markers:
        raise DirectGenerationIncompleteError(
            f"Direct generation markers incomplete ({len(present)}/{archive_count})"
        )

    files: dict[str, int] = {}
    for marker_name in sorted(expected_markers):
        raw = read_metadata_blob_text(
            container.get_blob_client(marker_name),
            max_bytes=1024 * 1024,
            label="direct-generation-marker",
        )
        try:
            marker = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DirectGenerationIncompleteError(
                "Direct generation marker was not valid JSON"
            ) from exc
        if marker.get("transfer_manifest_sha256") != transfer_sha:
            raise DirectGenerationIncompleteError("Direct generation marker transfer hash mismatch")
        entries = marker.get("files")
        if not isinstance(entries, list) or not entries:
            raise DirectGenerationIncompleteError("Direct generation marker had no extracted files")
        for entry in entries:
            if not isinstance(entry, dict):
                raise DirectGenerationIncompleteError(
                    "Direct generation marker file entry was invalid"
                )
            name = str(entry.get("name") or "")
            size = int(entry.get("size") or 0)
            if not name or "/" in name or size <= 0 or name in files:
                raise DirectGenerationIncompleteError(
                    "Direct generation marker file identity was invalid"
                )
            files[name] = size

    missing = []
    for name, size in files.items():
        blob_name = f"{data_prefix}/{name}"
        if blob_sizes.get(blob_name) != size:
            missing.append(name)
    if missing:
        raise DirectGenerationIncompleteError(
            f"Direct generation files incomplete ({len(missing)} missing/mismatched)"
        )
    return sorted(files), sum(files.values())


def _require_pending_contract(
    meta: dict[str, Any],
    *,
    operation_id: str,
    generation_id: str,
    data_prefix: str,
    transfer_sha: str,
    archive_count: int,
) -> dict[str, Any]:
    require_prepare_operation_owner(meta, operation_id)
    pending = meta.get("pending_generation")
    if not isinstance(pending, dict):
        raise DirectGenerationIncompleteError("Direct pending generation metadata is missing")
    expected = {
        "id": generation_id,
        "data_prefix": data_prefix,
        "transfer_manifest_sha256": transfer_sha,
        "archive_count": archive_count,
        "source_provider": "ncbi-direct",
    }
    if any(pending.get(key) != value for key, value in expected.items()):
        raise DirectGenerationIncompleteError("Direct pending generation metadata changed")
    return dict(pending)


def promote_direct_generation(
    *,
    credential: Any,
    storage_account: str,
    db_name: str,
    operation_id: str,
    generation_id: str,
    data_prefix: str,
    release: Mapping[str, Any],
    transfer_sha: str,
    archive_count: int,
    container: Any | None = None,
) -> dict[str, Any]:
    """Verify, shard, and atomically promote one persisted Direct generation."""
    if container is None:
        from api.services.storage.data import _blob_service

        container = _blob_service(credential, storage_account).get_container_client("blast-db")

    current, _etag = download_blob_with_etag(container, db_name)
    active = current.get("active_generation")
    if isinstance(active, dict) and active.get("id") == generation_id:
        return {
            "outcome": "already_promoted",
            "generation_id": generation_id,
            "files_total": int(current.get("file_count") or 0),
            "bytes_total": int(current.get("total_bytes") or 0),
        }

    def _checkpoint(phase: str) -> None:
        def _mutate(meta: dict[str, Any]) -> dict[str, Any]:
            pending = _require_pending_contract(
                meta,
                operation_id=operation_id,
                generation_id=generation_id,
                data_prefix=data_prefix,
                transfer_sha=transfer_sha,
                archive_count=archive_count,
            )
            pending["phase"] = phase
            pending["phase_updated_at"] = datetime.now(UTC).isoformat()
            meta["pending_generation"] = pending
            return meta

        update_metadata(container, db_name, storage_account, _mutate)

    _checkpoint("verifying")
    files, staged_bytes = verify_direct_generation(
        container,
        data_prefix=data_prefix,
        archive_count=archive_count,
        transfer_sha=transfer_sha,
    )
    _checkpoint("sharding")
    active_prefix = generation_db_prefix(db_name, generation_id)
    layout_prefix = f"{data_prefix}/shards"
    shard_summary = ensure_shard_sets(
        credential,
        storage_account,
        db_name,
        db_prefix=active_prefix,
        layout_prefix=layout_prefix,
    )
    try:
        shard_sets = require_complete_shard_summary(shard_summary)
    except RuntimeError as exc:
        raise DirectGenerationIncompleteError(str(exc)) from exc
    _checkpoint("promoting")

    letters = int(release.get("number_of_letters") or 0)
    sequences = int(release.get("number_of_sequences") or 0)
    if db_name != "taxdb" and (letters <= 0 or sequences <= 0):
        raise DirectGenerationIncompleteError("Direct searchable database counts are invalid")

    def _promote(meta: dict[str, Any]) -> dict[str, Any]:
        _require_pending_contract(
            meta,
            operation_id=operation_id,
            generation_id=generation_id,
            data_prefix=data_prefix,
            transfer_sha=transfer_sha,
            archive_count=archive_count,
        )
        previous = meta.get("active_generation")
        if previous:
            meta["previous_generation"] = previous
        now = datetime.now(UTC).isoformat()
        active_generation = {
            "id": generation_id,
            "prefix": active_prefix,
            "data_prefix": data_prefix,
            "source_provider": "ncbi-direct",
            "source_release_at": release["released_at"],
            "release_fingerprint": release["release_fingerprint"],
            "transfer_manifest_sha256": transfer_sha,
            "activated_at": now,
        }
        meta["active_generation"] = active_generation
        meta["active_prefix"] = active_prefix
        meta["shard_layout_prefix"] = layout_prefix
        meta["source_provider"] = "ncbi-direct"
        meta["source_release_at"] = release["released_at"]
        meta["release_fingerprint"] = release["release_fingerprint"]
        meta["transfer_manifest_sha256"] = transfer_sha
        meta["taxonomy_release_at"] = release.get("taxonomy_release_at")
        meta["taxonomy_release_fingerprint"] = release.get("taxonomy_release_fingerprint")
        meta["source_version"] = generation_id
        meta["downloaded_at"] = now
        meta["file_count"] = len(files)
        meta["total_bytes"] = staged_bytes
        meta["total_letters"] = letters
        meta["total_sequences"] = sequences
        meta["sharded"] = True
        meta["shard_sets"] = shard_sets
        meta["shard_source_version"] = generation_id
        meta["shard_layout_schema"] = int(shard_summary["layout_schema"])
        meta["sharded_at"] = now
        meta["update_in_progress"] = False
        meta["update_completed_at"] = now
        meta["copy_status"] = {
            "phase": "completed",
            "mode": "ncbi-direct",
            "total_files": len(files),
            "success": len(files),
            "failed": 0,
            "pending": 0,
        }
        meta.pop("pending_generation", None)
        meta.pop("prepare_operation_id", None)
        meta.pop("updating_to_source_version", None)
        meta.pop("update_error", None)
        meta.pop("update_failed_at", None)
        meta.pop("aks_job_ref", None)
        if isinstance(meta.get("db_order_oracle"), dict):
            oracle = dict(meta["db_order_oracle"])
            oracle["status"] = "stale"
            meta["db_order_oracle"] = oracle
        return meta

    update_metadata(container, db_name, storage_account, _promote)
    return {
        "outcome": "promoted",
        "generation_id": generation_id,
        "files_total": len(files),
        "bytes_total": staged_bytes,
    }


def recover_direct_generation(
    *,
    credential: Any,
    storage_account: str,
    db_name: str,
    metadata: Mapping[str, Any],
    container: Any | None = None,
) -> dict[str, Any]:
    """Promote a completed Direct Job from durable metadata after worker loss."""
    pending = metadata.get("pending_generation")
    ref = metadata.get("aks_job_ref")
    if not isinstance(pending, Mapping) or not isinstance(ref, Mapping):
        raise DirectGenerationIncompleteError("Direct recovery metadata is incomplete")
    operation_id = str(metadata.get("prepare_operation_id") or "")
    if not operation_id:
        raise DatabaseOperationOwnershipError("Direct recovery owner is missing")
    release = {
        "released_at": pending.get("source_release_at"),
        "release_fingerprint": pending.get("release_fingerprint"),
        "number_of_letters": pending.get("number_of_letters"),
        "number_of_sequences": pending.get("number_of_sequences"),
        "bytes_total": pending.get("bytes_total"),
        "taxonomy_release_at": pending.get("taxonomy_release_at"),
        "taxonomy_release_fingerprint": pending.get("taxonomy_release_fingerprint"),
    }
    required_strings = {
        "generation_id": pending.get("id"),
        "data_prefix": pending.get("data_prefix"),
        "transfer_sha": pending.get("transfer_manifest_sha256"),
        "released_at": release["released_at"],
        "release_fingerprint": release["release_fingerprint"],
    }
    if any(not isinstance(value, str) or not value for value in required_strings.values()):
        raise DirectGenerationIncompleteError("Direct recovery identity is incomplete")
    archive_count = int(pending.get("archive_count") or 0)

    from api.services.ncbi_direct_lock import (
        claim_or_refresh_direct_lock,
        release_direct_lock,
    )

    if not claim_or_refresh_direct_lock(operation_id):
        raise DirectRecoveryBusyError("Another NCBI Direct transfer owns the recovery lock")
    try:
        return promote_direct_generation(
            credential=credential,
            storage_account=storage_account,
            db_name=db_name,
            operation_id=operation_id,
            generation_id=str(required_strings["generation_id"]),
            data_prefix=str(required_strings["data_prefix"]),
            release=release,
            transfer_sha=str(required_strings["transfer_sha"]),
            archive_count=archive_count,
            container=container,
        )
    finally:
        try:
            release_direct_lock(operation_id)
        except Exception as exc:
            # Promotion is committed as one atomic Blob write. A Redis outage
            # after that commit must not hide the durable success from the
            # reconciler/JobState path; the expiring lock is only admission.
            LOGGER.warning(
                "NCBI Direct recovery lock release failed owner=%s: %s",
                operation_id,
                type(exc).__name__,
            )


__all__ = [
    "DirectGenerationIncompleteError",
    "DirectRecoveryBusyError",
    "promote_direct_generation",
    "recover_direct_generation",
    "verify_direct_generation",
]
