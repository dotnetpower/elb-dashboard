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

import copy
import json
import logging
import os
import re
import threading
import time
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
            from api.services.storage_data import read_metadata_blob_bytes

            data = read_metadata_blob_bytes(
                container.get_blob_client(f"{db_name}-metadata.json"),
                max_bytes=4 * 1024 * 1024,
                label="db-metadata.json",
            )
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

    Cached for ``BLAST_DB_METADATA_CACHE_TTL`` (default 24 h) keyed by
    ``(storage_account, db_name, database)``. Concurrent callers on a cache
    miss coordinate via a single-flight Event so only one of them pays the
    2-4 Storage blob round-trips; the rest reuse the result.
    """
    db_name = extract_db_name(database)
    if not db_name:
        return None

    cache_key = (storage_account or "", db_name, database or "")
    while True:
        now = time.monotonic()
        with _DISPLAY_METADATA_CACHE_LOCK:
            cached = _DISPLAY_METADATA_CACHE.get(cache_key)
            if cached and cached[0] > now:
                # Return a deep copy so caller mutations do not poison the
                # cache. Cost is negligible compared to the saved Storage
                # round-trips.
                return copy.deepcopy(cached[1])
            inflight = _DISPLAY_METADATA_INFLIGHT.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                _DISPLAY_METADATA_INFLIGHT[cache_key] = inflight
                leader = True
            else:
                leader = False
        if not leader:
            # Wait briefly for the leader to populate the cache, then retry
            # the lookup. Timeout matches the typical Storage round-trip
            # upper bound plus a safety margin; on timeout we fall through
            # to leader-elect ourselves rather than block forever.
            inflight.wait(timeout=15.0)
            continue
        try:
            info: dict[str, Any] = {}
            if storage_account:
                blastdb_json = resolve_blastdb_json_metadata(storage_account, db_name) or {}
                storage_metadata = resolve_db_metadata(storage_account, db_name) or {}
                info = {**blastdb_json, **storage_metadata}

            metadata = database_display_metadata_from_info(
                db_name, info, fallback_database=database
            )
            result = metadata or None
            expires_at = time.monotonic() + _DISPLAY_METADATA_CACHE_TTL_SECONDS
            with _DISPLAY_METADATA_CACHE_LOCK:
                _DISPLAY_METADATA_CACHE[cache_key] = (expires_at, result)
                if len(_DISPLAY_METADATA_CACHE) > 256:
                    oldest = min(
                        _DISPLAY_METADATA_CACHE.items(), key=lambda kv: kv[1][0]
                    )[0]
                    _DISPLAY_METADATA_CACHE.pop(oldest, None)
            return copy.deepcopy(result)
        finally:
            with _DISPLAY_METADATA_CACHE_LOCK:
                _DISPLAY_METADATA_INFLIGHT.pop(cache_key, None)
                inflight.set()


# DB display metadata changes only when an admin reruns ``prepare-db`` (or a
# `warmup_database` shard step rewrites the layout). Routes that mutate the
# underlying metadata.json blob MUST call ``invalidate_blast_db_metadata_cache``
# so the next read picks up the new value before the TTL expires. With
# explicit invalidation the TTL can be long (24 h) without serving stale data.
_DISPLAY_METADATA_CACHE_TTL_SECONDS = float(
    os.environ.get("BLAST_DB_METADATA_CACHE_TTL", "86400.0")
)
_DISPLAY_METADATA_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any] | None]] = {}
_DISPLAY_METADATA_CACHE_LOCK = threading.Lock()
_DISPLAY_METADATA_INFLIGHT: dict[tuple[str, str, str], threading.Event] = {}


def invalidate_blast_db_metadata_cache(
    storage_account: str | None = None,
    db_name: str | None = None,
) -> int:
    """Drop cached display metadata for one DB or for a whole account.

    - ``(account, db)`` both set: remove only the exact key.
    - ``account`` only: remove every entry for that account.
    - both ``None``: clear the cache entirely (equivalent to the test reset).

    Returns the number of entries removed. Safe to call from any sidecar:
    pure in-process state, no I/O, no Azure SDK touch. Cross-sidecar
    invalidation is handled separately (Redis pub/sub) when needed.
    """
    account_key = (storage_account or "").strip()
    db_key = (db_name or "").strip()
    with _DISPLAY_METADATA_CACHE_LOCK:
        if not account_key and not db_key:
            removed = len(_DISPLAY_METADATA_CACHE)
            _DISPLAY_METADATA_CACHE.clear()
            _DISPLAY_METADATA_INFLIGHT.clear()
            return removed
        to_drop: list[tuple[str, str, str]] = []
        for key in _DISPLAY_METADATA_CACHE:
            key_account, key_db, _key_db_input = key
            if account_key and key_account != account_key:
                continue
            if db_key and key_db != db_key:
                continue
            to_drop.append(key)
        for key in to_drop:
            _DISPLAY_METADATA_CACHE.pop(key, None)
        return len(to_drop)


def _reset_blast_db_metadata_cache() -> None:
    """Test hook: drop the cached merged display metadata."""

    invalidate_blast_db_metadata_cache()


# ---------------------------------------------------------------------------
# Cross-sidecar invalidation via Redis pub/sub.
#
# The cache is process-local. ``warmup_database`` (worker sidecar) and
# ``prepare-db`` (api sidecar) both rewrite ``{db}-metadata.json``, and a
# write in one process must drop the cache in the other. We use the existing
# ``OPS_REDIS_URL`` Redis db (already used by event_emitter, openapi_runtime,
# auto_warmup_reconcile) and a single channel.
#
# Reliability tier: best-effort, at-most-once. If the message is lost the
# TTL (default 24h) bounds the staleness; if pub/sub itself is unavailable
# the producer logs at debug and continues, never raising into the caller.
# ---------------------------------------------------------------------------
_INVALIDATE_CHANNEL = os.environ.get(
    "BLAST_DB_METADATA_INVALIDATE_CHANNEL",
    "elb:cache:blast-db-metadata",
)


def publish_blast_db_metadata_invalidate(
    storage_account: str | None = None,
    db_name: str | None = None,
) -> bool:
    """Best-effort Redis publish to invalidate the cache in peer sidecars.

    Returns ``True`` if the publish succeeded, ``False`` on any failure
    (including when ``BLAST_DB_METADATA_INVALIDATE_DISABLED=true``). Never
    raises — Redis being unreachable is a known degraded state we accept
    (TTL bounds the staleness window).
    """
    if os.environ.get("BLAST_DB_METADATA_INVALIDATE_DISABLED", "").lower() == "true":
        return False
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client(socket_timeout=1.5)
        payload = json.dumps(
            {"account": storage_account or "", "db": db_name or ""},
            separators=(",", ":"),
        )
        client.publish(_INVALIDATE_CHANNEL, payload)
        return True
    except Exception as exc:
        LOGGER.debug(
            "blast db metadata invalidate publish skipped: %s",
            type(exc).__name__,
        )
        return False


def notify_blast_db_metadata_changed(
    storage_account: str | None = None,
    db_name: str | None = None,
) -> None:
    """Local invalidate + cross-sidecar publish in one call.

    Routes and tasks that rewrite ``{db}-metadata.json`` should call this
    after the write so the very next read picks up the new value, regardless
    of which sidecar serves it.
    """
    invalidate_blast_db_metadata_cache(storage_account, db_name)
    publish_blast_db_metadata_invalidate(storage_account, db_name)


_INVALIDATE_SUBSCRIBER_THREAD: threading.Thread | None = None
_INVALIDATE_SUBSCRIBER_STOP: threading.Event | None = None
_INVALIDATE_SUBSCRIBER_LOCK = threading.Lock()


def start_invalidate_subscriber() -> threading.Thread | None:
    """Start the background pub/sub listener (idempotent).

    Spawned from the api sidecar's FastAPI lifespan. The worker / beat
    sidecars don't hold the cache so they don't subscribe — they only
    publish. Reconnects on Redis errors with exponential backoff capped
    at 30 s. Honours ``BLAST_DB_METADATA_INVALIDATE_DISABLED=true`` (set
    by tests so pytest never spawns the daemon thread).
    """
    if os.environ.get("BLAST_DB_METADATA_INVALIDATE_DISABLED", "").lower() == "true":
        return None
    global _INVALIDATE_SUBSCRIBER_THREAD, _INVALIDATE_SUBSCRIBER_STOP
    with _INVALIDATE_SUBSCRIBER_LOCK:
        if _INVALIDATE_SUBSCRIBER_THREAD is not None and _INVALIDATE_SUBSCRIBER_THREAD.is_alive():
            return _INVALIDATE_SUBSCRIBER_THREAD
        stop_event = threading.Event()

        def _run() -> None:
            from api.services.redis_clients import get_ops_redis_client

            backoff = 1.0
            while not stop_event.is_set():
                pubsub = None
                try:
                    client = get_ops_redis_client(socket_timeout=5)
                    pubsub = client.pubsub(ignore_subscribe_messages=True)
                    pubsub.subscribe(_INVALIDATE_CHANNEL)
                    backoff = 1.0
                    # Use get_message(timeout=1.0) instead of listen() so the
                    # stop_event is checked at least once per second. listen()
                    # would block forever on an idle connection and the
                    # subscriber thread would leak past shutdown.
                    while not stop_event.is_set():
                        message = pubsub.get_message(timeout=1.0)
                        if not message:
                            continue
                        data = message.get("data")
                        if not isinstance(data, (bytes, bytearray)):
                            continue
                        try:
                            payload = json.loads(bytes(data).decode("utf-8"))
                        except Exception as exc:
                            LOGGER.debug(
                                "blast db metadata invalidate payload decode skipped: %s",
                                type(exc).__name__,
                            )
                            continue
                        if not isinstance(payload, dict):
                            continue
                        account = (str(payload.get("account") or "")).strip() or None
                        db = (str(payload.get("db") or "")).strip() or None
                        invalidate_blast_db_metadata_cache(account, db)
                except Exception as exc:
                    LOGGER.info(
                        "blast db metadata invalidate subscriber retry: %s",
                        type(exc).__name__,
                    )
                    if stop_event.wait(timeout=backoff):
                        break
                    backoff = min(backoff * 2, 30.0)
                finally:
                    if pubsub is not None:
                        try:
                            pubsub.close()
                        except Exception as exc:
                            LOGGER.debug(
                                "blast db metadata invalidate pubsub close skipped: %s",
                                type(exc).__name__,
                            )

        _INVALIDATE_SUBSCRIBER_STOP = stop_event
        _INVALIDATE_SUBSCRIBER_THREAD = threading.Thread(
            target=_run,
            daemon=True,
            name="elb-cache-invalidate-sub",
        )
        _INVALIDATE_SUBSCRIBER_THREAD.start()
        return _INVALIDATE_SUBSCRIBER_THREAD


def stop_invalidate_subscriber(timeout: float = 1.0) -> None:
    """Signal the subscriber thread to exit. Best-effort join."""
    global _INVALIDATE_SUBSCRIBER_THREAD, _INVALIDATE_SUBSCRIBER_STOP
    with _INVALIDATE_SUBSCRIBER_LOCK:
        stop_event = _INVALIDATE_SUBSCRIBER_STOP
        thread = _INVALIDATE_SUBSCRIBER_THREAD
        _INVALIDATE_SUBSCRIBER_STOP = None
        _INVALIDATE_SUBSCRIBER_THREAD = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


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
        from api.services.storage_data import read_metadata_blob_bytes

        for blob_name in (
            f"{db_name}/{db_name}.njs",
            f"{db_name}.njs",
            f"custom_db/{db_name}/{db_name}.njs",
        ):
            try:
                data = read_metadata_blob_bytes(
                    container.get_blob_client(blob_name), label="blast-db-njs"
                )
                payload = json.loads(data.decode("utf-8"))
                if isinstance(payload, dict):
                    return _blastdb_json_info(payload)
            except ResourceNotFoundError:
                continue
            except ValueError:
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
