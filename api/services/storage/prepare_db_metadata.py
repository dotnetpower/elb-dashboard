"""prepare-db metadata.json read-modify-write helpers.

Pure storage data-plane logic for the per-database ``<db>-metadata.json``
control blob that the prepare-db routes maintain. Extracted from
`api/routes/storage/prepare_db.py` so the route keeps HTTP validation /
response shaping and this layer owns the reusable ETag-aware blob I/O the
route header's edit boundary says belongs in a service.

Responsibility: Read, write, and optimistic-concurrency-update the
    ``<db>-metadata.json`` blob, plus classify a stale ``update_in_progress``
    marker left by a crashed previous daemon.
Edit boundaries: Storage blob I/O only — no HTTP, no Celery dispatch, no
    NCBI listing. The route still owns dispatch, locking, and error mapping.
Key entry points: `read_db_metadata`, `download_blob_with_etag`,
    `write_db_metadata`, `update_metadata`, `is_stale_prepare_marker`.
Risky contracts: `update_metadata` MUST stay read-modify-write with ETag
    ``If-Match`` retry so a concurrent writer (shard daemon, warmup task)
    cannot clobber unrelated fields; its final blind write is the documented
    last-resort. `write_db_metadata` MUST invalidate the merged display cache
    so the SPA never shows stale state after a prepare-db action.
Validation: `uv run pytest -q api/tests/test_storage_data.py
    api/tests/test_prepare_db_hardening.py api/tests/test_prepare_db_routes.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

LOGGER = logging.getLogger(__name__)

# An older `update_in_progress=true` marker than this is treated as a crashed
# previous daemon. Large enough to cover the worst-case copy initiation for
# `nt`/`sra` but small enough that real crashes recover same-hour.
_PREPARE_DB_STALE_SECONDS = 2 * 60 * 60

__all__ = [
    "_PREPARE_DB_STALE_SECONDS",
    "download_blob_with_etag",
    "is_stale_prepare_marker",
    "read_db_metadata",
    "update_metadata",
    "write_db_metadata",
]


def is_stale_prepare_marker(metadata: dict[str, Any]) -> bool:
    """Return True if the metadata's update_in_progress flag is old enough to
    be treated as a crashed previous daemon."""
    if not metadata.get("update_in_progress"):
        return True
    started = str(metadata.get("update_started_at") or "")
    if not started:
        return True
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - started_dt).total_seconds()
    except Exception:
        return True
    return age >= _PREPARE_DB_STALE_SECONDS


def read_db_metadata(container: Any, db_name: str) -> dict[str, Any]:
    metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
    try:
        from api.services.storage.data import read_metadata_blob_text

        payload = read_metadata_blob_text(
            metadata_blob, max_bytes=4 * 1024 * 1024, label="db-metadata.json"
        )
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        LOGGER.debug("DB metadata read skipped for %s: %s", db_name, type(exc).__name__)
    return {"db_name": db_name}


def download_blob_with_etag(container: Any, db_name: str) -> tuple[dict[str, Any], str]:
    """Read metadata + its current ETag for optimistic concurrency writes."""
    blob = container.get_blob_client(f"{db_name}-metadata.json")
    try:
        # ``length=`` here caps the download server-side; we accept the
        # `etag` lookup may run twice (download then properties) but the
        # blob is small (<4 MiB) so latency is fine.
        max_bytes = 4 * 1024 * 1024
        stream = blob.download_blob(offset=0, length=max_bytes + 1)
        payload_bytes = stream.readall()
        if len(payload_bytes) > max_bytes:
            LOGGER.warning(
                "db-metadata.json blob exceeds %d bytes (got %d); treating as missing",
                max_bytes,
                len(payload_bytes),
            )
            return {"db_name": db_name}, ""
        payload = payload_bytes.decode("utf-8")
        try:
            parsed = json.loads(payload) if payload else {}
            if not isinstance(parsed, dict):
                parsed = {"db_name": db_name}
        except json.JSONDecodeError:
            parsed = {"db_name": db_name}
        etag = ""
        try:
            etag = getattr(stream, "properties", None).etag if stream.properties else ""  # type: ignore[union-attr]
        except Exception:
            etag = ""
        return parsed, etag or ""
    except ResourceNotFoundError:
        return {"db_name": db_name}, ""
    except Exception as exc:
        LOGGER.debug("DB metadata read skipped for %s: %s", db_name, type(exc).__name__)
        return {"db_name": db_name}, ""


def write_db_metadata(
    container: Any,
    db_name: str,
    payload: dict[str, Any],
    *,
    account_name: str,
    etag: str | None = None,
) -> str:
    """Write metadata.json. When ``etag`` is set, the upload uses
    ``If-Match`` so a concurrent writer cannot clobber unrelated fields.
    Returns the resulting blob's new ETag (or empty string)."""
    metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
    kwargs: dict[str, Any] = {"overwrite": True}
    if etag:
        kwargs["etag"] = etag
        kwargs["match_condition"] = MatchConditions.IfNotModified
    result = metadata_blob.upload_blob(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        **kwargs,
    )
    # Drop the merged display-metadata cache so /api/blast/jobs/{id} picks up
    # the new title / sequence count / sharded badge on the next read instead
    # of waiting up to ``BLAST_DB_METADATA_CACHE_TTL`` (default 24 h). Invoked
    # for every prepare-db write — start, success, failure — so the UI never
    # shows stale state after an admin action. Best-effort: cache invalidation
    # must not fail the metadata write. ``notify_*`` also publishes to the
    # Redis pub/sub channel so the worker / beat sidecars (and any peer api
    # replica) drop their copies too.
    try:
        from api.services.blast.db_metadata import notify_blast_db_metadata_changed

        notify_blast_db_metadata_changed(account_name, db_name)
    except Exception as exc:
        LOGGER.debug(
            "db metadata cache invalidate skipped db=%s: %s",
            db_name,
            type(exc).__name__,
        )
    if isinstance(result, dict):
        return str(result.get("etag", "") or "").strip('"')
    return ""


def update_metadata(
    container: Any,
    db_name: str,
    account_name: str,
    mutator: Any,
    *,
    max_attempts: int = 5,
) -> dict[str, Any]:
    """Read-modify-write metadata.json with ETag retry.

    ``mutator(meta_copy) -> dict`` must be a pure function over the snapshot
    (it should not depend on external state). On 412 Precondition Failed
    (concurrent writer) we re-read and retry up to ``max_attempts``. Final
    fall-back is a blind overwrite — only reached when the concurrent writer
    is itself in a loop, which we accept rather than failing the caller.
    """
    last: dict[str, Any] = {}
    for attempt in range(max_attempts):
        current, etag = download_blob_with_etag(container, db_name)
        try:
            mutated = mutator(dict(current))
        except Exception:
            raise
        try:
            write_db_metadata(
                container,
                db_name,
                mutated,
                account_name=account_name,
                etag=etag or None,
            )
            return mutated
        except ResourceModifiedError:
            LOGGER.debug(
                "metadata ETag retry db=%s attempt=%d",
                db_name,
                attempt + 1,
            )
            last = mutated
            continue
        except Exception:
            raise
    # Final blind write — we already exhausted retries; better to land the
    # mutation and accept that a peer's interleaved field may be lost than to
    # leave the metadata silently un-updated.
    write_db_metadata(container, db_name, last, account_name=account_name)
    return last
