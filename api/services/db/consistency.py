"""BLAST DB Storage volume/shard consistency reconciliation (prune + re-shard).

Heals the 3-way generation mismatch that arises when NCBI SHRINKS a database
(e.g. core_nt 94 -> 79 volumes): prepare-db copies the new generation's files
but never prunes the stale "ghost" volume blobs left from the larger snapshot,
nor regenerates the shard alias layout for the new volume set. The result is a
Storage state where the db-level metadata (``<db>.njs`` / ``<db>.ndb`` LMDB)
knows N volumes, the volume files number M > N, and the ``Kshards/`` alias layout
references volumes that no longer belong to the DB. ``blastdbcmd -db <shard>
-info`` then fails with "Input db vol does not match lmdb vol", which cascades
into every BLAST job on that DB failing with the coarse "one or more BLAST jobs
failed".

The authoritative volume count is the BLAST v5 ``<db>.njs``
``number-of-volumes`` field (written by NCBI/makeblastdb). Any Storage volume
whose numeric index is >= that count is a ghost.

Responsibility: Detect + heal Storage volume/shard inconsistency for ONE DB:
    read the authoritative njs volume count, prune ghost volume blobs, and
    regenerate the shard alias layout for the true volume set. Pure Storage
    data-plane work via the shared MI credential.
Edit boundaries: No HTTP shaping and no Celery scheduling here — routes/tasks
    call ``reconcile_db_consistency``. Reuses ``api.services.db.sharding``
    primitives (``list_db_volumes``, ``ensure_shard_sets``).
Key entry points: ``read_authoritative_volume_count``, ``find_ghost_volumes``,
    ``prune_ghost_volumes``, ``delete_shard_layouts``,
    ``shard_layout_needs_rebuild``, ``reconcile_db_consistency``.
Risky contracts: DELETES Storage blobs. Guarded so it can NEVER delete when the
    njs authority is missing / unparseable / <= 0, and ABORTS (deletes nothing)
    when ghosts would exceed ``_MAX_GHOST_FRACTION`` of all volumes (a defensive
    stop against an NCBI latest-dir glitch that under-reports the count). It does
    NOT take the per-DB prepare-db lock itself — callers that can race prepare-db
    (the beat reconciler) MUST hold ``prepare_db_lock`` around the call. All work
    is best-effort and returns a structured summary; it never raises.
Validation: ``uv run pytest -q api/tests/test_db_consistency.py``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.db.sharding import (
    DEFAULT_CONTAINER,
    PRESET_SHARD_SETS,
    _validate_db_name,
    ensure_shard_sets,
    list_db_volumes,
)

LOGGER = logging.getLogger(__name__)

# Defensive cap: if pruning would remove more than this fraction of all volumes,
# treat it as a likely NCBI latest-dir glitch (under-reported njs count) rather
# than a genuine shrink, and ABORT instead of deleting. A real shrink drops a few
# trailing volumes; a glitch would look like "delete most of the DB".
_MAX_GHOST_FRACTION = 0.5

# Volume-file leaf pattern: ``core_nt.79.nsq`` -> group(1)=``core_nt.79``.
_VOL_FILE_RE_TMPL = r"^({db}\.\d+)\.[a-z]{{2,4}}$"


def _volume_index(basename: str, db_name: str) -> int:
    """Return the numeric volume index of a basename, or 0 for the single-vol form.

    ``core_nt.79`` -> 79; ``core_nt`` (single-volume DB) -> 0.
    """
    m = re.match(rf"^{re.escape(db_name)}\.(\d+)$", basename)
    return int(m.group(1)) if m else 0


def read_authoritative_volume_count(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
) -> int | None:
    """Read ``number-of-volumes`` from the BLAST v5 ``<db>.njs`` (NCBI authority).

    Returns the volume count, or ``None`` when the njs is missing / unparseable /
    lacks a positive count. ``None`` means "no authority" and every caller MUST
    treat it as "do not prune" — deleting on a missing authority would be data
    loss. Never raises.
    """
    _validate_db_name(db_name)
    try:
        from api.services.storage.data import _blob_service, read_metadata_blob_bytes

        svc = _blob_service(credential, account_name)
        cc = svc.get_container_client(container)
        bc = cc.get_blob_client(f"{db_name}/{db_name}.njs")
        raw = read_metadata_blob_bytes(bc, label="blast-db-njs")
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        LOGGER.info("db-consistency: no njs authority for %s: %s", db_name, type(exc).__name__)
        return None
    count = data.get("number-of-volumes") if isinstance(data, dict) else None
    if isinstance(count, (int, float)) and int(count) > 0:
        return int(count)
    return None


def find_ghost_volumes(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
) -> tuple[int | None, list[str], int]:
    """Return ``(authoritative_count, ghost_basenames, actual_volume_count)``.

    A ghost is a Storage volume whose numeric index is >= the njs count. Returns
    ``(None, [], actual)`` when there is no njs authority (callers must not prune)
    and ``(count, [], 0)`` when the storage volume listing fails. Never raises.
    """
    count = read_authoritative_volume_count(credential, account_name, db_name, container=container)
    try:
        volumes, _ = list_db_volumes(credential, account_name, db_name, container=container)
    except Exception as exc:
        LOGGER.info(
            "db-consistency: list_db_volumes failed for %s: %s",
            db_name,
            type(exc).__name__,
        )
        return (count, [], 0)
    actual = len(volumes)
    if count is None:
        return (None, [], actual)
    ghosts = [v for v in volumes if _volume_index(v, db_name) >= count]
    return (count, ghosts, actual)


def prune_ghost_volumes(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
) -> dict[str, Any]:
    """Delete ghost volume blobs (index >= njs count). Safety-capped.

    Deletes NOTHING when there is no njs authority, no ghosts, or the ghost
    fraction exceeds ``_MAX_GHOST_FRACTION`` (defensive abort). Returns a
    structured summary. Never raises.
    """
    count, ghosts, actual = find_ghost_volumes(
        credential, account_name, db_name, container=container
    )
    if count is None:
        return {"status": "skipped", "reason": "no_njs_authority", "pruned": 0}
    if not ghosts:
        return {
            "status": "clean",
            "authoritative": count,
            "actual": actual,
            "pruned": 0,
        }
    if actual and len(ghosts) > actual * _MAX_GHOST_FRACTION:
        LOGGER.warning(
            "db-consistency: REFUSING to prune %d/%d ghost volumes for %s "
            "(> %.0f%% — likely an NCBI latest-dir glitch; manual check needed)",
            len(ghosts),
            actual,
            db_name,
            _MAX_GHOST_FRACTION * 100,
        )
        return {
            "status": "aborted",
            "reason": "too_many_ghosts",
            "authoritative": count,
            "actual": actual,
            "ghost_volumes": len(ghosts),
            "pruned": 0,
        }

    from api.services.storage.data import _blob_service

    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    leaf_re = re.compile(_VOL_FILE_RE_TMPL.format(db=re.escape(db_name)))
    ghost_set = set(ghosts)
    deleted = 0
    try:
        for blob in cc.list_blobs(name_starts_with=f"{db_name}/"):
            name = getattr(blob, "name", "") or ""
            leaf = name.rsplit("/", 1)[-1]
            m = leaf_re.match(leaf)
            if m and m.group(1) in ghost_set:
                try:
                    cc.delete_blob(name)
                    deleted += 1
                except Exception as exc:
                    LOGGER.warning(
                        "db-consistency: ghost blob delete failed %s: %s",
                        name,
                        type(exc).__name__,
                    )
    except Exception as exc:
        LOGGER.warning(
            "db-consistency: ghost enumeration failed for %s: %s",
            db_name,
            type(exc).__name__,
        )
    LOGGER.info(
        "db-consistency: pruned %d ghost blobs (%d volumes) for %s (authoritative=%d)",
        deleted,
        len(ghosts),
        db_name,
        count,
    )
    return {
        "status": "pruned",
        "authoritative": count,
        "actual": actual,
        "ghost_volumes": len(ghosts),
        "pruned": deleted,
    }


def delete_shard_layouts(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
    presets: tuple[int, ...] = PRESET_SHARD_SETS,
) -> int:
    """Delete every preset shard alias layout (``Kshards/<db>_shard_*``).

    ``upload_shard_set`` is skip-if-exists and never deletes, so a stale layout
    built for a different volume count survives a re-shard. Deleting first forces
    ``ensure_shard_sets`` to rebuild cleanly on the current volume set. Returns
    the number of blobs deleted. Never raises.
    """
    from api.services.storage.data import _blob_service

    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    deleted = 0
    for n in sorted({int(p) for p in presets}):
        prefix = f"{n}shards/{db_name}_shard_"
        try:
            for blob in cc.list_blobs(name_starts_with=prefix):
                name = getattr(blob, "name", "") or ""
                try:
                    cc.delete_blob(name)
                    deleted += 1
                except Exception:  # noqa: S110 - best-effort cleanup, next tick retries
                    pass
        except Exception as exc:
            LOGGER.debug(
                "db-consistency: shard layout list failed N=%d db=%s: %s",
                n,
                db_name,
                type(exc).__name__,
            )
    return deleted


def shard_layout_needs_rebuild(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    authoritative_count: int,
    *,
    container: str = DEFAULT_CONTAINER,
    presets: tuple[int, ...] = PRESET_SHARD_SETS,
) -> bool:
    """True if any shard alias references a volume index >= ``authoritative_count``.

    Detects a stale layout even when NO ghost volumes remain (e.g. a prune
    succeeded but the follow-up re-shard failed on a previous pass). Reads the
    ``.nal`` blobs of the largest applicable preset and parses their DBLIST for
    the highest volume index. Returns ``False`` (do not rebuild) on any read
    failure so a transient Storage hiccup cannot trigger churn. Never raises.
    """
    from api.services.storage.data import _blob_service

    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    vol_re = re.compile(rf"{re.escape(db_name)}\.(\d+)\b")
    # Largest preset that could exist gives the finest-grained shards, most
    # likely to reference the highest volume — check that one.
    for n in sorted({int(p) for p in presets}, reverse=True):
        prefix = f"{n}shards/{db_name}_shard_"
        found_any = False
        try:
            for blob in cc.list_blobs(name_starts_with=prefix):
                name = getattr(blob, "name", "") or ""
                if not name.endswith(".nal"):
                    continue
                found_any = True
                try:
                    from api.services.storage.data import read_metadata_blob_text

                    text = read_metadata_blob_text(
                        cc.get_blob_client(name), max_bytes=64 * 1024, label="shard-nal"
                    )
                except Exception:  # noqa: S112 - unreadable alias skipped, not fatal
                    continue
                for m in vol_re.finditer(text):
                    if int(m.group(1)) >= authoritative_count:
                        return True
        except Exception:
            return False
        if found_any:
            # This preset existed and referenced no out-of-range volume → clean.
            return False
    # No shard layout present at all → not "stale", just unsharded.
    return False


def reconcile_db_consistency(
    credential: TokenCredential,
    account_name: str,
    db_name: str,
    *,
    container: str = DEFAULT_CONTAINER,
    force_reshard: bool = False,
) -> dict[str, Any]:
    """Detect + heal Storage volume/shard inconsistency for one DB.

    1. Prune ghost volumes (safety-capped; skipped when no njs authority).
    2. Re-shard when ghosts were pruned, OR the existing shard layout still
       references out-of-range volumes (a stale layout from a failed prior pass),
       OR ``force_reshard`` is set. Deletes the stale layout first, then
       ``ensure_shard_sets`` rebuilds it on the true post-prune volume set.

    Callers that can race prepare-db (the beat reconciler) MUST hold
    ``prepare_db_lock(account_name, db_name)`` around this call. Returns a
    structured summary; never raises.
    """
    _validate_db_name(db_name)
    summary: dict[str, Any] = {"db": db_name}
    prune = prune_ghost_volumes(credential, account_name, db_name, container=container)
    summary["prune"] = prune
    if prune.get("status") == "aborted":
        summary["status"] = "aborted"
        return summary

    pruned = prune.get("status") == "pruned"
    authoritative = prune.get("authoritative")
    stale_layout = False
    if not pruned and isinstance(authoritative, int) and authoritative > 0:
        stale_layout = shard_layout_needs_rebuild(
            credential, account_name, db_name, authoritative, container=container
        )
        summary["stale_shard_layout"] = stale_layout

    if pruned or stale_layout or force_reshard:
        try:
            deleted = delete_shard_layouts(credential, account_name, db_name, container=container)
            shard = ensure_shard_sets(credential, account_name, db_name, container=container)
            summary["shard_layouts_deleted"] = deleted
            summary["shard"] = {
                "total_volumes": shard.get("total_volumes"),
                "shard_sets": shard.get("shard_sets"),
                "errors": shard.get("errors"),
            }
            summary["resharded"] = True
        except Exception as exc:
            LOGGER.warning("db-consistency: reshard failed for %s: %s", db_name, type(exc).__name__)
            summary["resharded"] = False
            summary["shard_error"] = type(exc).__name__
    else:
        summary["resharded"] = False

    if prune.get("status") == "skipped":
        summary["status"] = "skipped"
    elif pruned:
        summary["status"] = "healed"
    elif stale_layout:
        summary["status"] = "reshard_only"
    else:
        summary["status"] = "clean"
    return summary


def reconcile_all_db_consistency(
    credential: TokenCredential,
    *,
    limit: int = 200,
    storage_account: str | None = None,
    container: str = DEFAULT_CONTAINER,
) -> dict[str, Any]:
    """Reconcile volume/shard consistency for every prepared DB (beat entry point).

    Iterates each root-level ``{db}-metadata.json`` and, for each DB, acquires
    the per-DB prepare-db lock NON-BLOCKING (skips a DB whose prepare-db is
    running this tick, so the reconciler can never race a live download) then
    runs ``reconcile_db_consistency`` with ``force_reshard=False`` — it only
    re-shards when a ghost was actually pruned or the layout is provably stale.
    This is the self-heal path: a DB that drifted (a prune that succeeded but
    whose follow-up reshard failed, or a shrink that happened outside a
    prepare-db run) is repaired within one beat cycle. Never raises; returns a
    per-DB summary. The Celery task that schedules this is gated default-OFF
    (charter §12a Rule 4) so enabling automatic self-heal is an explicit opt-in.
    """
    from api.services.storage.data import _blob_service
    from api.services.storage.orphan_prepare_db import (
        _iter_metadata_db_names,
        _resolve_workload_storage_account,
    )
    from api.services.storage.prepare_db_locks import prepare_db_lock

    account = storage_account or _resolve_workload_storage_account()
    if not account:
        return {"status": "skipped", "reason": "no_storage_account", "checked": 0, "healed": 0}
    try:
        svc = _blob_service(credential, account)
        cc = svc.get_container_client(container)
    except Exception as exc:
        LOGGER.info("db-consistency reconcile-all: storage unavailable: %s", type(exc).__name__)
        return {"status": "skipped", "reason": "storage_unavailable", "checked": 0, "healed": 0}

    checked = 0
    healed = 0
    aborted = 0
    details: list[dict[str, Any]] = []
    for db_name in _iter_metadata_db_names(cc, limit=limit):
        checked += 1
        lock = prepare_db_lock(account, db_name)
        if not lock.acquire(blocking=False):
            continue  # prepare-db (or another reconcile) is active — skip this tick
        try:
            recon = reconcile_db_consistency(credential, account, db_name, container=container)
        except Exception as exc:
            LOGGER.warning(
                "db-consistency reconcile-all failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
            continue
        finally:
            lock.release()
        status = recon.get("status")
        if status in ("healed", "reshard_only"):
            healed += 1
            details.append({"db": db_name, "status": status, "prune": recon.get("prune")})
        elif status == "aborted":
            aborted += 1
            details.append({"db": db_name, "status": status, "prune": recon.get("prune")})
    if healed or aborted:
        LOGGER.info(
            "db-consistency reconcile-all: account=%s checked=%d healed=%d aborted=%d",
            account,
            checked,
            healed,
            aborted,
        )
    return {
        "status": "ok",
        "account": account,
        "checked": checked,
        "healed": healed,
        "aborted": aborted,
        "details": details,
    }
