"""Reconcile orphaned AKS-fanout prepare-db state back to a terminal phase.

Responsibility: Reconcile orphaned prepare-db metadata after worker/revision loss: recover a
    fully staged NCBI Direct generation through immutable validation and atomic promotion, or
    drive missing/failed partial work to a terminal phase so the SPA and operation gate converge.
Edit boundaries: Pure-Python decision + Storage/Kubernetes read-modify-write only. Do NOT
    re-dispatch downloads from here (no auto-relaunch — that belongs to an explicit user
    Update click). Metadata writes go through the shared ETag-guarded ``update_metadata``
    helper so a concurrent fresh dispatch is never clobbered. Never open Storage network surface.
Key entry points: ``reconcile_orphaned_prepare_db`` (orchestrator),
    ``classify_prepare_db_entry`` (pure decision function, unit-tested in isolation).
Risky contracts: The authoritative orphan signal is the K8s Job lookup, NOT age — a healthy
    ``nt`` download can run for many hours, so age-only resets would abort
    live downloads. Age is used only as a fallback when no ``aks_job_ref`` is recorded
    (server-side mode). The reset mutator re-validates both ``update_started_at`` and the
    owner token under the ETag so an interleaved new dispatch is never reset. NCBI Direct
    recovery terminalizes only ``pending_generation`` and preserves active copy/shard state.
    A completed Direct Job gets a grace window for its original worker, then recovery uses the
    persisted owner/release/hash/count contract; an absent Redis lock may be reclaimed only by
    that same durable owner.
Validation: ``uv run pytest -q api/tests/test_orphan_prepare_db_reconcile.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

# Phases that mean "a download is still being driven". Anything outside this
# set (completed / partial / failed / cancelled) is terminal and left alone.
# An empty phase is treated as non-terminal because every dispatch writes
# ``phase="queued"`` first; a missing phase therefore implies an odd/old row
# that should still be eligible for the authoritative Job check.
_NON_TERMINAL_PHASES = frozenset(
    {"", "queued", "copying", "downloading", "running", "verifying", "sharding", "promoting"}
)

_METADATA_SUFFIX = "-metadata.json"


class _SkipReset(Exception):
    """Raised inside the reset mutator when the row changed under us
    (a fresh dispatch replaced the orphan) so the write is abandoned."""


def _marker_age_seconds(metadata: dict[str, Any], *, now: datetime) -> float | None:
    """Return the in-progress marker age in seconds, or ``None`` if the
    ``update_started_at`` timestamp is missing/unparseable."""
    started = str(metadata.get("update_started_at") or "")
    if not started:
        return None
    try:
        started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except Exception:
        return None
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return (now - started_dt).total_seconds()


def _job_has_failed(job_status: dict[str, Any]) -> bool:
    """True when the K8s Job carries a ``Failed`` condition set to ``True``
    (backoffLimit exceeded / activeDeadline). A merely-elevated ``failed`` pod
    counter is NOT treated as terminal because the Job may still be retrying."""
    for cond in job_status.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if str(cond.get("type", "")).lower() == "failed" and (
            str(cond.get("status", "")).lower() == "true"
        ):
            return True
    return False


def classify_prepare_db_entry(
    metadata: dict[str, Any],
    job_status: dict[str, Any] | None,
    *,
    now: datetime,
    stale_seconds: float,
    direct_recovery_grace_seconds: float = 120.0,
) -> tuple[str, str]:
    """Decide what to do with a single ``{db}-metadata.json`` row.

    Pure function (no IO) so every branch is unit-testable. ``job_status`` is
    the result of :func:`get_prepare_db_job` for the row's ``aks_job_ref``,
    or ``None`` when there is no ref or the lookup failed.

    Returns ``(action, reason)`` where ``action`` is one of
    ``"recover-direct"`` (validate/promote a completed Direct generation),
    ``"reset"`` (drive to terminal partial), ``"skip-running"`` (Job alive),
    ``"skip-recent"`` (no ref, marker still within the stale window),
    ``"skip-terminal"`` (already terminal / not in progress), or
    ``"skip-error"`` (ref present but Job lookup unavailable — do not guess).
    """
    if not metadata.get("update_in_progress"):
        return ("skip-terminal", "update_in_progress is not set")

    copy_status = metadata.get("copy_status")
    phase = ""
    if isinstance(copy_status, dict):
        phase = str(copy_status.get("phase") or "").lower()
    ref = metadata.get("aks_job_ref")
    isolated_direct = bool(isinstance(ref, dict) and ref.get("source_provider") == "ncbi-direct")
    pending_generation = metadata.get("pending_generation")
    if isolated_direct and isinstance(pending_generation, dict):
        phase = str(pending_generation.get("phase") or "").lower()
    if phase not in _NON_TERMINAL_PHASES:
        return ("skip-terminal", f"copy_status.phase={phase!r} is terminal")

    has_ref = isinstance(ref, dict) and bool(ref.get("job_name")) and bool(ref.get("cluster_name"))
    job_name = str(ref.get("job_name") or "") if isinstance(ref, dict) else ""

    if has_ref:
        if job_status is None:
            # Ref present but the Job could not be queried (transient AKS/API
            # error). Resetting here could abort a live download, so wait for
            # a future tick when the lookup succeeds.
            return ("skip-error", "AKS Job lookup unavailable")
        live_status = job_status
        if int(live_status.get("status_code") or 200) != 200:
            return ("skip-error", "AKS Job lookup returned a non-success status")
        if isolated_direct:
            pending = pending_generation if isinstance(pending_generation, dict) else {}
            archive_count = int(pending.get("archive_count") or 0)
            succeeded = int(live_status.get("succeeded") or 0)
            if archive_count > 0 and succeeded >= archive_count:
                completion_text = str(live_status.get("completion_time") or "")
                if completion_text:
                    try:
                        completion = datetime.fromisoformat(completion_text.replace("Z", "+00:00"))
                        if completion.tzinfo is None:
                            completion = completion.replace(tzinfo=UTC)
                        completion_age = (now - completion).total_seconds()
                    except (TypeError, ValueError):
                        completion_age = -1.0
                    if 0 <= completion_age < direct_recovery_grace_seconds:
                        return (
                            "skip-running",
                            "completed Direct Job is inside the "
                            f"{direct_recovery_grace_seconds:.0f}s recovery grace",
                        )
                elif not live_status.get("missing"):
                    # Do not race the original worker when Kubernetes omitted
                    # the completion timestamp. The Job TTL eventually turns
                    # this into the unambiguous missing-Job recovery branch.
                    return ("skip-running", "completed Direct Job has no completion timestamp")
                return ("recover-direct", "completed Direct Job needs generation promotion")
            if live_status.get("missing"):
                # The Job may have completed and then expired before the first
                # post-revision reconcile tick. Marker verification decides
                # whether this is promotable or a terminal partial generation.
                return ("recover-direct", "Direct Job is gone; verify persisted generation markers")
        if live_status.get("missing"):
            return (
                "reset",
                f"AKS Job {job_name} no longer exists and the "
                "polling task did not record a terminal state",
            )
        if _job_has_failed(live_status):
            return ("reset", f"AKS Job {job_name} failed")
        # Job is present and not Failed (active / complete / retrying). A live
        # poller owns the lifecycle; do not interfere.
        return ("skip-running", f"AKS Job {job_name} still present")

    # No AKS job ref recorded (server-side mode, or a dispatch that crashed
    # before persisting the ref). The K8s Job check is unavailable, so fall
    # back to the age-based stale window — the same threshold the route uses.
    age = _marker_age_seconds(metadata, now=now)
    if age is None or age >= stale_seconds:
        return (
            "reset",
            "stale in-progress marker with no AKS job ref",
        )
    return ("skip-recent", f"marker age {age:.0f}s < stale window {stale_seconds:.0f}s")


def _resolve_workload_storage_account() -> str:
    """Resolve the single workload Storage account from the deployment env."""
    for name in ("STORAGE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    endpoint = os.environ.get("AZURE_BLOB_ENDPOINT", "").strip()
    if endpoint:
        host = urlparse(endpoint).netloc or endpoint.removeprefix("https://").split("/", 1)[0]
        account = host.split(".", 1)[0].strip()
        if account:
            return account
    return ""


def _iter_metadata_db_names(container: Any, *, limit: int) -> Iterator[str]:
    """Yield root-level DB names that have a ``{db}-metadata.json`` blob.

    Uses ``walk_blobs(delimiter="/")`` so the per-DB ``<db>/`` data folders are
    collapsed into single prefixes instead of enumerating tens of thousands of
    data blobs every tick. Falls back to ``list_blobs`` for fakes/SDKs that do
    not implement ``walk_blobs``.
    """
    try:
        items = container.walk_blobs(delimiter="/")
    except (TypeError, AttributeError):
        items = container.list_blobs()
    seen = 0
    for item in items:
        name = str(getattr(item, "name", "") or "")
        if "/" in name:
            # A ``<db>/`` folder prefix (BlobPrefix) or a nested blob — skip.
            continue
        if not name.endswith(_METADATA_SUFFIX):
            continue
        db_name = name[: -len(_METADATA_SUFFIX)]
        if not db_name:
            continue
        yield db_name
        seen += 1
        if seen >= limit:
            return


def reconcile_orphaned_prepare_db(
    *,
    credential: Any,
    storage_account: str | None = None,
    container: Any = None,
    job_lookup: Callable[..., dict[str, Any]] | None = None,
    now: datetime | None = None,
    stale_seconds: float | None = None,
    direct_recovery_grace_seconds: float | None = None,
    direct_recover: Callable[..., dict[str, Any]] | None = None,
    limit: int = 200,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Scan ``blast-db`` metadata and drive orphaned prepare-db rows to terminal.

    Side effects: reads Storage metadata + AKS Job status; rewrites
    ``{db}-metadata.json`` (``update_in_progress=False`` + ``copy_status.phase=
    "partial"`` + drops ``aks_job_ref``) for rows whose driving Job is gone or
    failed. Never re-dispatches a download and never opens Storage network.

    Idempotent: a reset row leaves a terminal phase, so the next tick classifies
    it ``skip-terminal``. Concurrency-safe: the reset is an ETag-guarded write
    that re-checks ``update_started_at`` so an interleaved fresh dispatch is
    skipped, not clobbered.
    """
    if enabled is None:
        enabled = (
            os.environ.get("PREPARE_DB_ORPHAN_RECONCILE_ENABLED", "true").strip().lower() != "false"
        )
    if not enabled:
        return {"enabled": False, "reset": [], "scanned": 0}

    # Deferred imports keep the import graph free of route<->service cycles and
    # avoid importing azure/k8s SDKs at module import time (tests inject fakes).
    from api.services.storage.prepare_db_metadata import (
        _PREPARE_DB_STALE_SECONDS,
        invalidate_shard_publication,
    )
    from api.services.storage.prepare_db_metadata import (
        download_blob_with_etag as _download_blob_with_etag,
    )
    from api.services.storage.prepare_db_metadata import (
        update_metadata as _update_metadata,
    )
    from api.tasks.storage.prepare_db_via_aks import _count_staged_blobs

    if stale_seconds is None:
        stale_seconds = float(_PREPARE_DB_STALE_SECONDS)
    if now is None:
        now = datetime.now(UTC)
    if direct_recovery_grace_seconds is None:
        try:
            configured_grace = float(
                os.environ.get("PREPARE_DB_DIRECT_RECOVERY_GRACE_SECONDS", "120")
            )
        except ValueError:
            configured_grace = 120.0
        direct_recovery_grace_seconds = min(max(configured_grace, 30.0), 3600.0)

    account = (storage_account or _resolve_workload_storage_account()).strip()
    if not account:
        return {"enabled": True, "skipped": "no-storage-account", "reset": [], "scanned": 0}

    if container is None:
        from api.services.storage.data import _blob_service

        container = _blob_service(credential, account).get_container_client("blast-db")

    if job_lookup is None:
        from api.services.k8s.prepare_db_jobs import get_prepare_db_job

        job_lookup = get_prepare_db_job

    result: dict[str, Any] = {
        "enabled": True,
        "account": account,
        "scanned": 0,
        "reset": [],
        "skipped_running": [],
        "skipped_recent": [],
        "skipped_terminal": [],
        "skipped_error": [],
        "skipped_raced": [],
        "recovered_direct": [],
        "errors": [],
    }

    for db_name in _iter_metadata_db_names(container, limit=limit):
        result["scanned"] += 1
        try:
            metadata, _etag = _download_blob_with_etag(container, db_name)
        except Exception as exc:  # pragma: no cover - SDK variance
            LOGGER.debug("orphan reconcile read skipped db=%s: %s", db_name, type(exc).__name__)
            result["errors"].append(db_name)
            continue

        # Cheap pre-filter so we only pay the AKS Job lookup for live markers.
        if not metadata.get("update_in_progress"):
            result["skipped_terminal"].append(db_name)
            continue

        ref = metadata.get("aks_job_ref")
        has_ref = (
            isinstance(ref, dict) and bool(ref.get("job_name")) and bool(ref.get("cluster_name"))
        )
        job_status: dict[str, Any] | None = None
        if has_ref:
            try:
                job_status = job_lookup(
                    credential,
                    str(ref.get("subscription_id") or ""),
                    str(ref.get("resource_group") or ""),
                    str(ref.get("cluster_name") or ""),
                    namespace=str(ref.get("namespace") or "default"),
                    job_name=str(ref.get("job_name") or ""),
                )
            except Exception as exc:
                LOGGER.info(
                    "orphan reconcile Job lookup failed db=%s job=%s: %s",
                    db_name,
                    ref.get("job_name"),
                    type(exc).__name__,
                )
                job_status = None

        action, reason = classify_prepare_db_entry(
            metadata,
            job_status,
            now=now,
            stale_seconds=stale_seconds,
            direct_recovery_grace_seconds=direct_recovery_grace_seconds,
        )

        if action == "skip-running":
            result["skipped_running"].append(db_name)
            continue
        if action == "skip-recent":
            result["skipped_recent"].append(db_name)
            continue
        if action == "skip-terminal":
            result["skipped_terminal"].append(db_name)
            continue
        if action == "skip-error":
            result["skipped_error"].append(db_name)
            continue

        if action == "recover-direct":
            if direct_recover is None:
                from api.services.storage.direct_promotion import (
                    recover_direct_generation as direct_recover_impl,
                )

                direct_recover = direct_recover_impl
            try:
                recovered = direct_recover(
                    credential=credential,
                    storage_account=account,
                    db_name=db_name,
                    metadata=metadata,
                    container=container,
                )
            except Exception as exc:
                from api.services.storage.direct_promotion import (
                    DirectGenerationIncompleteError,
                    DirectRecoveryBusyError,
                )
                from api.services.storage.prepare_db_metadata import (
                    DatabaseOperationOwnershipError,
                )

                if isinstance(exc, DatabaseOperationOwnershipError):
                    result["skipped_raced"].append(db_name)
                    continue
                if isinstance(exc, DirectRecoveryBusyError):
                    result["skipped_error"].append(db_name)
                    continue
                if isinstance(exc, DirectGenerationIncompleteError):
                    reason = f"Direct recovery validation failed: {exc}"
                    action = "reset"
                else:
                    LOGGER.warning(
                        "orphan reconcile Direct recovery deferred db=%s: %s",
                        db_name,
                        type(exc).__name__,
                    )
                    result["errors"].append(db_name)
                    continue
            else:
                pending = metadata.get("pending_generation")
                job_id = str(pending.get("job_id") or "") if isinstance(pending, dict) else ""
                cleanup: dict[str, Any] | None = None
                if isinstance(ref, dict):
                    try:
                        from api.services.k8s.prepare_db_jobs import delete_prepare_db_job

                        cleanup = delete_prepare_db_job(
                            credential,
                            str(ref.get("subscription_id") or ""),
                            str(ref.get("resource_group") or ""),
                            str(ref.get("cluster_name") or ""),
                            namespace=str(ref.get("namespace") or "default"),
                            job_name=str(ref.get("job_name") or ""),
                            configmap_name=str(
                                ref.get("configmap_name") or ref.get("job_name") or ""
                            )
                            or None,
                        )
                    except Exception as cleanup_exc:
                        LOGGER.warning(
                            "orphan reconcile recovered Direct cleanup failed db=%s job=%s: %s",
                            db_name,
                            ref.get("job_name"),
                            type(cleanup_exc).__name__,
                        )
                result["recovered_direct"].append(
                    {
                        "db_name": db_name,
                        "job_id": job_id,
                        **recovered,
                        "aks_cleanup": cleanup,
                    }
                )
                LOGGER.info(
                    "orphan reconcile promoted Direct generation db=%s generation=%s",
                    db_name,
                    recovered.get("generation_id"),
                )
                continue

        # action == "reset"
        observed_started_at = str(metadata.get("update_started_at") or "")
        observed_operation_id = str(metadata.get("prepare_operation_id") or "")
        isolated_direct = bool(
            isinstance(ref, dict) and ref.get("source_provider") == "ncbi-direct"
        )
        staged = _count_staged_blobs(container, db_name)
        success = staged[0] if staged else None
        copy_status = metadata.get("copy_status")
        total_files = copy_status.get("total_files") if isinstance(copy_status, dict) else None
        mode_label = "aks" if has_ref else "server-side"

        def _reset_mutator(
            meta: dict[str, Any],
            *,
            _reason: str = reason,
            _started: str = observed_started_at,
            _operation_id: str = observed_operation_id,
            _mode: str = mode_label,
            _success: int | None = success,
            _total: Any = total_files,
            _isolated_direct: bool = isolated_direct,
        ) -> dict[str, Any]:
            # Re-validate under the ETag — a fresh dispatch may have replaced
            # this orphan between our read and this write.
            if not meta.get("update_in_progress"):
                raise _SkipReset
            cs = meta.get("copy_status")
            phase = str(cs.get("phase") or "").lower() if isinstance(cs, dict) else ""
            if _isolated_direct:
                pending = meta.get("pending_generation")
                if isinstance(pending, dict):
                    phase = str(pending.get("phase") or "").lower()
            if phase not in _NON_TERMINAL_PHASES:
                raise _SkipReset
            if str(meta.get("update_started_at") or "") != _started:
                raise _SkipReset
            if _operation_id and str(meta.get("prepare_operation_id") or "") != _operation_id:
                raise _SkipReset
            meta["update_in_progress"] = False
            meta.pop("prepare_operation_id", None)
            meta.pop("cancel_operation_id", None)
            meta.pop("cancel_started_at", None)
            meta["update_error"] = f"prepare-db reconciler: {_reason}"
            meta["update_failed_at"] = now.isoformat()
            summary: dict[str, Any] = {
                "phase": "partial",
                "mode": _mode,
                "reason": _reason,
                "reconciled": True,
            }
            if _total is not None:
                summary["total_files"] = _total
            if _success is not None:
                summary["success"] = _success
            if _isolated_direct:
                pending = dict(meta.get("pending_generation") or {})
                pending.update(summary)
                meta["pending_generation"] = pending
            else:
                meta["copy_status"] = summary
            meta.pop("aks_job_ref", None)
            meta.pop("updating_to_source_version", None)
            if _isolated_direct:
                return meta
            return dict(
                invalidate_shard_publication(
                    meta,
                    error=f"prepare-db reconciler: {_reason}",
                )
            )

        try:
            _update_metadata(container, db_name, account, _reset_mutator)
        except _SkipReset:
            LOGGER.info("orphan reconcile raced (fresh dispatch) db=%s", db_name)
            result["skipped_raced"].append(db_name)
            continue
        except Exception as exc:
            LOGGER.warning(
                "orphan reconcile reset write failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
            result["errors"].append(db_name)
            continue

        LOGGER.info("orphan reconcile reset db=%s reason=%s", db_name, reason)
        if isolated_direct and observed_operation_id:
            try:
                from api.services.ncbi_direct_lock import release_direct_lock

                release_direct_lock(observed_operation_id)
            except Exception as exc:
                LOGGER.warning(
                    "orphan reconcile Direct lock release failed db=%s: %s",
                    db_name,
                    type(exc).__name__,
                )
        result["reset"].append(db_name)

    return result
