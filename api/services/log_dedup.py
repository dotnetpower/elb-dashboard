"""Per-key TTL-window log deduplication.

Responsibility: Provide a single ``dedup_log_warning`` helper that the rest
of the api/worker code can call to avoid flooding App Insights / log
streams with the same WARNING line per polling tick.
Edit boundaries: Stdlib only. No FastAPI / Celery / Azure SDK imports.
Callers must remain free of this module's internal state.
Key entry points: ``dedup_log_warning``, ``reset_dedup_state``.
Risky contracts: The dedup window must be short enough that a genuinely
NEW outage class still surfaces quickly (default 300 s) but long enough
to absorb a per-poll WARNING burst (>=60 s). The state is process-local;
multi-replica deployments emit one WARNING per replica per window.
Validation: ``uv run pytest -q api/tests/test_log_dedup.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Hashable

DEFAULT_DEDUP_WINDOW_SECONDS = 300.0
_MAX_TRACKED_KEYS = 1024

_LOCK = threading.Lock()
_LAST_EMITTED: dict[Hashable, float] = {}
_INSERT_ORDER: deque[Hashable] = deque()


def dedup_log_warning(
    logger: logging.Logger,
    dedup_key: Hashable,
    message: str,
    *args: object,
    window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS,
    exc_info: bool = False,
) -> None:
    """Log ``message`` at WARNING the first time per window; DEBUG on repeat.

    ``dedup_key`` is any hashable that uniquely identifies the failure class
    (e.g. ``("aks_warmup_status", cluster_name, type(exc).__name__)``).
    The first emission inside a fresh window calls ``logger.warning``;
    every subsequent identical key inside the window drops to ``logger.debug``
    with a ``"(deduped)"`` marker so operators can still inspect the
    full burst locally without it landing in App Insights exception rows.

    ``window_seconds`` defaults to 300 s — long enough to absorb a
    dashboard's monitor-poll burst (every 5-30 s), short enough that a
    genuinely sustained new outage still produces a fresh WARNING within
    a few minutes.

    ``exc_info=True`` is forwarded so the first emission still records a
    stack trace (and therefore an App Insights exception row); repeats
    do not — that's exactly what we want.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    emit_warning = True
    with _LOCK:
        # Evict expired keys from the head of the insertion deque so the
        # tracked-key cap stays meaningful in a long-lived process.
        while _INSERT_ORDER:
            head = _INSERT_ORDER[0]
            last = _LAST_EMITTED.get(head)
            if last is None or last < cutoff:
                _INSERT_ORDER.popleft()
                _LAST_EMITTED.pop(head, None)
                continue
            break
        last = _LAST_EMITTED.get(dedup_key)
        if last is not None and last >= cutoff:
            emit_warning = False
        else:
            _LAST_EMITTED[dedup_key] = now
            _INSERT_ORDER.append(dedup_key)
            # Belt-and-braces hard cap so a runaway caller with unique keys
            # cannot grow the map without bound.
            while len(_INSERT_ORDER) > _MAX_TRACKED_KEYS:
                oldest = _INSERT_ORDER.popleft()
                _LAST_EMITTED.pop(oldest, None)
    if emit_warning:
        logger.warning(message, *args, exc_info=exc_info)
    else:
        logger.debug(message + " (deduped)", *args)


def reset_dedup_state() -> None:
    """Clear all dedup state. Test-only — do not call from production code."""
    with _LOCK:
        _LAST_EMITTED.clear()
        _INSERT_ORDER.clear()
