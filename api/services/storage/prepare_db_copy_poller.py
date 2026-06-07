"""prepare-db server-side copy.status polling.

Long-running data-plane helper that polls every staged blob's
``copy.status`` until each reaches a terminal state, so the prepare-db route's
daemon can record an honest partial / timed-out / completed outcome instead of
the pre-hardening "file_count >= 90%" heuristic. Extracted from
`api/routes/storage/prepare_db.py` so the route keeps HTTP concerns and this
layer owns the reusable polling loop.

Responsibility: Poll the staged blobs' `copy.status` to a terminal state and
    return the `{success, failed, aborted, pending, failed_files, timed_out,
    elapsed_seconds}` summary, honouring a cooperative shutdown event.
Edit boundaries: Storage blob polling only — no HTTP, no metadata write (the
    caller supplies an `on_progress` callback for that), no NCBI listing.
Key entry points: `poll_copy_completion`, `request_shutdown`,
    `_COPY_POLL_INTERVAL_SECONDS`, `_COPY_POLL_MAX_SECONDS`.
Risky contracts: A blob with no copy metadata (pre-existing shard alias) MUST
    count as success, never hang the loop. `timed_out` is True only when work
    is still pending AND shutdown was not signalled, so a clean drain is never
    mis-reported as a timeout. Tests monkeypatch `_COPY_POLL_INTERVAL_SECONDS`
    / `_COPY_POLL_MAX_SECONDS` ON THIS MODULE — keep them module-level.
Validation: `uv run pytest -q api/tests/test_prepare_db_hardening.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

LOGGER = logging.getLogger(__name__)

# Cooperative shutdown event — checked by ``poll_copy_completion`` between
# batches so the api sidecar can exit cleanly on SIGTERM instead of waiting
# the full poll interval. The event stays unset in normal operation; the
# main process can call ``request_shutdown()`` from its lifespan shutdown
# hook (not yet wired — opt-in).
_SHUTDOWN_EVENT = threading.Event()
# Copy-status poll cadence and cap. The api sidecar wakes once a minute to
# poll BlobProperties.copy.status for every staged file. Bounded so that an
# orphaned daemon cannot run indefinitely; on hitting the cap we mark the
# update failed with timed_out reason so the SPA shows an honest state.
_COPY_POLL_INTERVAL_SECONDS = 60.0
_COPY_POLL_MAX_SECONDS = 24 * 60 * 60
_COPY_POLL_BATCH_SIZE = max(32, int(os.environ.get("PREPARE_DB_COPY_POLL_BATCH_SIZE", "256")))

__all__ = [
    "_COPY_POLL_INTERVAL_SECONDS",
    "_COPY_POLL_MAX_SECONDS",
    "poll_copy_completion",
    "request_shutdown",
    "shutdown_requested",
]


def request_shutdown() -> None:
    """Signal in-flight poll loops to drain early (lifespan shutdown hook)."""
    _SHUTDOWN_EVENT.set()


def shutdown_requested() -> bool:
    """Return True when a cooperative shutdown has been signalled."""
    return _SHUTDOWN_EVENT.is_set()


def poll_copy_completion(
    container: Any,
    blob_names: list[str],
    *,
    db_name: str,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Poll each staged blob's copy.status until every copy reaches a terminal
    state. Returns ``{success, failed, aborted, pending, failed_files,
    timed_out, elapsed_seconds}``.

    Why this exists: ``start_copy_from_url`` only ACKs that Azure accepted the
    copy job; the actual transfer happens asynchronously and can fail with
    ``copy.status='failed'`` (NCBI throttling, source 404 mid-snapshot,
    transient network blip). The pre-hardening UI inferred completion from
    the file_count >= 90% heuristic which let partial successes show as
    "Ready" — see Critique items 6 & 7.
    """
    pending = set(blob_names)
    success = 0
    failed = 0
    aborted = 0
    failed_files: list[dict[str, str]] = []
    deadline = time.monotonic() + _COPY_POLL_MAX_SECONDS
    while pending and time.monotonic() < deadline:
        prefix = f"{db_name}/"
        copy_include_supported = True
        try:
            try:
                blobs = container.list_blobs(name_starts_with=prefix, include=["copy"])
            except TypeError:
                copy_include_supported = False
                blobs = container.list_blobs(name_starts_with=prefix)
            copy_by_name = {str(blob.name): getattr(blob, "copy", None) for blob in blobs}
        except Exception as exc:
            LOGGER.debug(
                "copy status batch probe failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
            copy_by_name = None
        # Iterate in deterministic order so logs stay diff-friendly.
        for name in sorted(pending)[:_COPY_POLL_BATCH_SIZE]:
            if copy_by_name is None:
                continue
            if name not in copy_by_name:
                # The destination blob disappeared (eg an admin deleted the
                # container mid-flight). Surface as a copy failure rather
                # than hanging forever.
                failed += 1
                failed_files.append(
                    {"blob": name, "status": "missing", "reason": "blob not found"}
                )
                pending.discard(name)
                continue
            copy = copy_by_name[name]
            if copy is None and not copy_include_supported:
                try:
                    copy = getattr(
                        container.get_blob_client(name).get_blob_properties(),
                        "copy",
                        None,
                    )
                except ResourceNotFoundError:
                    failed += 1
                    failed_files.append(
                        {"blob": name, "status": "missing", "reason": "blob not found"}
                    )
                    pending.discard(name)
                    continue
                except Exception as exc:
                    LOGGER.debug(
                        "copy status fallback probe failed db=%s blob=%s: %s",
                        db_name,
                        name,
                        type(exc).__name__,
                    )
                    continue
            status = ""
            description = ""
            if copy is not None:
                status = str(getattr(copy, "status", "") or "").lower()
                description = str(getattr(copy, "status_description", "") or "")
            if not status:
                # No copy metadata = the blob existed before this prepare_db
                # call (e.g. shard alias). Treat as success.
                success += 1
                pending.discard(name)
                continue
            if status == "success":
                success += 1
                pending.discard(name)
            elif status == "failed":
                failed += 1
                failed_files.append(
                    {"blob": name, "status": "failed", "reason": description[:200]}
                )
                pending.discard(name)
            elif status == "aborted":
                aborted += 1
                failed_files.append(
                    {"blob": name, "status": "aborted", "reason": description[:200]}
                )
                pending.discard(name)
            # "pending" stays in the set for the next sweep.
        if pending:
            if callable(on_progress):
                try:
                    on_progress(
                        {
                            "success": success,
                            "failed": failed,
                            "aborted": aborted,
                            "pending": len(pending),
                        }
                    )
                except Exception as exc:
                    LOGGER.debug(
                        "copy progress callback raised db=%s: %s",
                        db_name,
                        type(exc).__name__,
                    )
            # Cooperative sleep — Event.wait returns True if shutdown was
            # signalled, in which case we exit the poll loop early so the
            # api sidecar can drain before its grace period ends. The next
            # process restart picks the work up via stale-flag recovery.
            if _SHUTDOWN_EVENT.wait(timeout=_COPY_POLL_INTERVAL_SECONDS):
                LOGGER.info(
                    "copy poll exiting early on shutdown db=%s pending=%d",
                    db_name,
                    len(pending),
                )
                break
    timed_out = bool(pending) and not _SHUTDOWN_EVENT.is_set()
    return {
        "success": success,
        "failed": failed,
        "aborted": aborted,
        "pending": len(pending),
        "failed_files": failed_files[:50],
        "timed_out": timed_out,
        "elapsed_seconds": int(time.monotonic() - (deadline - _COPY_POLL_MAX_SECONDS)),
    }
