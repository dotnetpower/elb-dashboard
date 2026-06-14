"""Service Bus telemetry — derived stats over the raw admin counts.

Responsibility: Single-purpose helpers that turn point-in-time Service Bus
    runtime counts into derived, time-aware stats the dashboard needs but the
    SDK does not provide directly. Today this is the DLQ growth-rate window
    (an in-process rolling sample of the last ``_WINDOW_SECONDS`` of
    dead-letter counts, used by the Message Flow card to surface a DLQ
    alarm). Stays separate from ``service_bus.py`` so that module remains a
    thin admin/data-plane SDK wrapper and history-aware logic has one
    obvious home to extend (transfer-DLQ growth, throughput rate, etc.).
Edit boundaries: No HTTP, no FastAPI, no direct ``azure.servicebus`` import
    (the raw count must be passed in). Pure in-process aggregation — process
    restart loses history by design, which is acceptable for a best-effort
    operator hint. Routes/services pass in a count; this module returns the
    derived shape.
Key entry points: ``record_dlq_sample``, ``dlq_delta``, ``reset_for_tests``.
Risky contracts: The sample deque is bounded by ``_MAX_SAMPLES`` so a long
    uptime cannot leak memory. Samples are keyed by ``namespace_fqdn`` +
    ``queue`` so a config swap in Settings starts a fresh history instead of
    blending two namespaces. The delta is a FLOOR (only counts what we have
    actually observed since process start), never extrapolated — a freshly
    started api sidecar reports ``samples=1`` and a delta of zero so the SPA
    does not show a misleading spike.
Validation: ``uv run pytest -q api/tests/test_service_bus_telemetry.py``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

# Rolling window: last hour of DLQ samples. Long enough to catch a slow leak,
# short enough to avoid keeping stale data after a DLQ purge.
_WINDOW_SECONDS = 3600
# Cap the deque so a fast poller (one sample every few seconds for an hour)
# stays well under 1 KB per queue. 720 entries = one sample every 5 s for the
# full window, more than enough resolution for a UI hint.
_MAX_SAMPLES = 720


@dataclass(frozen=True)
class DlqDelta:
    """Result of a DLQ growth-rate query.

    Fields are intentionally simple so the SPA can render them without a
    second client-side computation step.
    """

    window_seconds: int
    """Configured rolling window (always ``_WINDOW_SECONDS``)."""

    samples: int
    """Number of stored samples that fell inside the window (>=1 after the
    first ``record_dlq_sample`` call)."""

    baseline_dlq: int
    """Oldest in-window DLQ count we have observed (the floor for the delta)."""

    current_dlq: int
    """Most recent DLQ count we recorded."""

    delta: int
    """``current_dlq - baseline_dlq`` clamped at zero. Negative deltas (a
    purge or an operator-driven manual settle) are reported as zero growth so
    the SPA's alarm threshold cannot fire on a healing queue."""

    elapsed_seconds: float
    """How long the window of samples actually covers. Less than
    ``window_seconds`` shortly after a restart; the SPA uses this to label
    the alarm honestly ('+12 since restart' vs '+12 in 1h')."""


# (namespace_fqdn, queue) -> deque[(epoch_seconds, count)]
_HISTORY: dict[tuple[str, str], deque[tuple[float, int]]] = {}
_LOCK = threading.Lock()


def _now() -> float:
    """Wall-clock seconds, factored out for tests."""
    return time.time()


def _key(namespace_fqdn: str, queue: str) -> tuple[str, str]:
    return ((namespace_fqdn or "").strip().lower(), (queue or "").strip())


def _trim(samples: deque[tuple[float, int]], *, now: float) -> None:
    """Drop samples older than the window. Always run before reading."""
    cutoff = now - _WINDOW_SECONDS
    while samples and samples[0][0] < cutoff:
        samples.popleft()


def record_dlq_sample(namespace_fqdn: str, queue: str, dlq_count: int) -> None:
    """Append a DLQ observation to the rolling window.

    Silently no-ops when ``dlq_count`` is not a non-negative int — the count
    is a best-effort signal and a bad SDK response must never pollute the
    history. The Message Flow snapshot route calls this every time it
    successfully reads ``entity_counts``.
    """
    if not isinstance(dlq_count, int) or isinstance(dlq_count, bool) or dlq_count < 0:
        return
    if not namespace_fqdn:
        return
    key = _key(namespace_fqdn, queue)
    now = _now()
    with _LOCK:
        bucket = _HISTORY.get(key)
        if bucket is None:
            bucket = deque(maxlen=_MAX_SAMPLES)
            _HISTORY[key] = bucket
        bucket.append((now, dlq_count))
        _trim(bucket, now=now)


def dlq_delta(namespace_fqdn: str, queue: str) -> DlqDelta | None:
    """Return the DLQ growth-rate stats for a queue, or ``None`` when there
    is no in-window history (first call before ``record_dlq_sample``)."""
    if not namespace_fqdn:
        return None
    key = _key(namespace_fqdn, queue)
    now = _now()
    with _LOCK:
        bucket = _HISTORY.get(key)
        if not bucket:
            return None
        _trim(bucket, now=now)
        if not bucket:
            return None
        oldest_ts, baseline = bucket[0]
        _, current = bucket[-1]
        delta = max(0, current - baseline)
        return DlqDelta(
            window_seconds=_WINDOW_SECONDS,
            samples=len(bucket),
            baseline_dlq=baseline,
            current_dlq=current,
            delta=delta,
            elapsed_seconds=max(0.0, now - oldest_ts),
        )


def reset_for_tests() -> None:
    """Clear the rolling-window history. Test-only entry point."""
    with _LOCK:
        _HISTORY.clear()
