"""Per-(account, db) prepare-db lock registry.

Process-local mutual-exclusion registry that stops a re-clicked Download from
spawning two daemons that race the same ``<db>-metadata.json`` blob. Extracted
from `api/routes/storage/prepare_db.py` so the route keeps HTTP concerns and
this layer owns the reusable concurrency primitive.

Responsibility: Hand out a stable `threading.Lock` per `(account, db)` pair,
    with a bounded soft-GC of currently-unlocked entries so memory stays
    capped for deployments that prepare hundreds of databases over time.
Edit boundaries: Pure in-process concurrency — no Azure SDK, no IO, no HTTP.
Key entry points: `prepare_db_lock`.
Risky contracts: A currently-locked entry is NEVER evicted, so a live
    daemon's lock can never be silently lost. Cross-process serialisation is
    still the metadata's ``update_in_progress`` flag — this lock only guards a
    single api replica.
Validation: `uv run pytest -q api/tests/test_prepare_db_routes.py
    api/tests/test_prepare_db_hardening.py`.
"""

from __future__ import annotations

import threading

# Per-(account, db) lock registry. Mirrors the pattern used by
# /api/blast/databases/{db}/shard so a re-clicked Download cannot spawn two
# daemons that race the same metadata.json blob.
#
# Soft-GC: the registry caps at ``_PREPARE_DB_LOCK_REGISTRY_MAX`` entries and
# evicts any currently-unlocked entry when full. This keeps memory bounded
# even for deployments that prepare hundreds of custom databases over time.
_PREPARE_DB_LOCK_REGISTRY: dict[str, threading.Lock] = {}
_PREPARE_DB_LOCK_REGISTRY_GUARD = threading.Lock()
_PREPARE_DB_LOCK_REGISTRY_MAX = 256

__all__ = ["prepare_db_lock"]


def prepare_db_lock(account_name: str, db_name: str) -> threading.Lock:
    key = f"{account_name.lower()}|{db_name}"
    with _PREPARE_DB_LOCK_REGISTRY_GUARD:
        lock = _PREPARE_DB_LOCK_REGISTRY.get(key)
        if lock is not None:
            return lock
        # Soft GC — evict any free locks if we're at the cap. Locked entries
        # are kept so a live daemon's lock is never silently lost.
        if len(_PREPARE_DB_LOCK_REGISTRY) >= _PREPARE_DB_LOCK_REGISTRY_MAX:
            for stale_key in list(_PREPARE_DB_LOCK_REGISTRY):
                candidate = _PREPARE_DB_LOCK_REGISTRY[stale_key]
                if candidate.acquire(blocking=False):
                    candidate.release()
                    _PREPARE_DB_LOCK_REGISTRY.pop(stale_key, None)
                    if (
                        len(_PREPARE_DB_LOCK_REGISTRY)
                        < _PREPARE_DB_LOCK_REGISTRY_MAX
                    ):
                        break
        lock = threading.Lock()
        _PREPARE_DB_LOCK_REGISTRY[key] = lock
        return lock
