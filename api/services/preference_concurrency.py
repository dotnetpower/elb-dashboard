"""Shared optimistic-concurrency primitives for preference tables.

Responsibility: Provide a typed conflict exception and a bounded CAS retry
    helper used by ``auto_stop`` and ``auto_warmup`` (and any future
    preference table that ships read-modify-write code paths) so the same
    contract — fresh-read, build, conditional save, refresh-and-retry on
    conflict, surface on exhaustion — is implemented once.
Edit boundaries: Storage-layer adjunct only. No FastAPI, Celery, Azure SDK,
    or Kubernetes-API imports here so this module stays trivially testable
    and importable from any preference helper.
Key entry points: ``PreferenceUpdateConflict``, ``cas_retry``.
Risky contracts: The retry helper deliberately bounds attempts (default 5)
    and re-raises ``PreferenceUpdateConflict`` after exhausting them — it
    does NOT silently swallow the conflict, because that would re-introduce
    the lost-update class this primitive exists to prevent. Callers that
    want a "best-effort, log and return stale" behaviour wrap the call in
    their own ``try`` and surface a structured warning.
Validation: ``uv run pytest -q api/tests/test_preference_concurrency.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

LOGGER = logging.getLogger(__name__)

DEFAULT_CAS_MAX_ATTEMPTS = 5


class PreferenceUpdateConflict(RuntimeError):
    """Raised when an Azure Tables If-Match update races a concurrent writer.

    Surfaced by the preference helpers' ``_save_*`` backends and by the
    ``cas_retry`` helper after its bounded attempt budget is exhausted.
    Catch it at the boundary that owns the user request (e.g. the route
    handler) to translate it into a 409 Conflict, or — for background
    bookkeeping writes such as ``mark_auto_stop_event`` — log and skip
    the update so the in-memory snapshot is not silently persisted on
    top of the freshly-written row.
    """


def cas_retry[T](
    attempt: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_CAS_MAX_ATTEMPTS,
    operation: str = "preference_cas",
) -> T:
    """Run ``attempt`` and retry while it raises ``PreferenceUpdateConflict``.

    ``attempt`` is a zero-argument callable that performs one full
    read-modify-conditional-write cycle. It MUST raise
    ``PreferenceUpdateConflict`` (not silently overwrite) on an ETag
    mismatch — the helper assumes that contract and surfaces the same
    exception after exhausting ``max_attempts``.

    ``operation`` is a short label used in the exhaustion log line so
    operators can attribute lost-update warnings to a specific writer
    (``auto_stop.mark_event``, ``auto_warmup.mark_ready``, …).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last_exc: PreferenceUpdateConflict | None = None
    for attempt_index in range(1, max_attempts + 1):
        try:
            return attempt()
        except PreferenceUpdateConflict as exc:
            last_exc = exc
            if attempt_index < max_attempts:
                LOGGER.info(
                    "%s CAS retry %d/%d after ETag conflict: %s",
                    operation,
                    attempt_index,
                    max_attempts,
                    exc,
                )
    LOGGER.warning(
        "%s CAS retries exhausted after %d attempts; surfacing conflict",
        operation,
        max_attempts,
    )
    # ``cas_retry`` returns or raises; the loop above always sets ``last_exc``.
    # ``if/raise`` instead of ``assert`` so the guard survives Python ``-O``.
    if last_exc is None:  # pragma: no cover
        raise RuntimeError("cas_retry exhausted without recording an exception")
    raise last_exc


__all__ = (
    "DEFAULT_CAS_MAX_ATTEMPTS",
    "PreferenceUpdateConflict",
    "cas_retry",
)
