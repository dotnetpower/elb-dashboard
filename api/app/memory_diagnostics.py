"""Opt-in memory diagnostics sampler for the api sidecar.

Periodically samples and logs process memory so a *suspected* leak can be
confirmed as unbounded growth vs a bounded plateau, and optionally returns
freed glibc arenas to the OS via ``malloc_trim``. Entirely opt-in — when
``API_MEMTRACE_INTERVAL_SECONDS`` is unset / <= 0 this module starts nothing
and has zero runtime cost, so it is safe to ship enabled-by-default-OFF.

Responsibility: Provide a single daemon-thread memory sampler (RSS + GC stats +
optional ``tracemalloc`` top-N) plus a best-effort ``malloc_trim`` mitigation,
started from the api lifespan.
Edit boundaries: Diagnostics only. Stay stdlib-only; do not import route/service
business logic. Must be a no-op when disabled.
Key entry points: ``start_memory_sampler``, ``read_rss_bytes``, ``sample_once``,
``malloc_trim``.
Risky contracts: The sampler loop must NEVER raise out (diagnostics must not
crash the sidecar). ``tracemalloc`` adds allocation-tracking overhead so it is
gated separately from the base RSS/GC sample.
Validation: `uv run pytest -q api/tests/test_memory_diagnostics.py`.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

_DEFAULT_TOPN = 5
# Clamp the sample interval so a fat-fingered override can neither hammer the
# log (too small) nor silently disable a requested sampler (parsing crash).
_MIN_INTERVAL_SECONDS = 5.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        LOGGER.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def read_rss_bytes() -> int | None:
    """Return the process resident set size in bytes, or ``None`` off-Linux.

    Reads ``/proc/self/status`` (the same source the cgroup reporter uses) so
    the sampler stays dependency-free.
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def malloc_trim() -> bool:
    """Best-effort ``malloc_trim(0)`` to hand freed glibc arenas back to the OS.

    Returns ``True`` when the call succeeded. A no-op / ``False`` on musl or any
    libc without ``malloc_trim`` (never raises).
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=False)
        libc.malloc_trim(0)
        return True
    except Exception:
        return False


def sample_once(
    *,
    tracemalloc_top: int = 0,
    trim: bool = False,
) -> dict[str, Any]:
    """Take one memory sample and emit a structured log line.

    Returns the sampled metrics so tests (and callers) can assert on them
    without parsing logs. ``tracemalloc_top > 0`` additionally logs the top-N
    allocation sources (requires ``tracemalloc`` to have been started).
    """
    rss = read_rss_bytes()
    gc_counts = gc.get_count()
    metrics: dict[str, Any] = {
        "rss_bytes": rss,
        "gc_count": list(gc_counts),
        "gc_objects": len(gc.get_objects()),
    }
    trimmed = malloc_trim() if trim else None
    if trimmed is not None:
        metrics["malloc_trimmed"] = trimmed
        # Re-read RSS after the trim so the log shows the reclaimed delta.
        rss_after = read_rss_bytes()
        metrics["rss_bytes_after_trim"] = rss_after
    LOGGER.info(
        "memtrace rss=%s gc_count=%s gc_objects=%d%s",
        rss,
        gc_counts,
        metrics["gc_objects"],
        (
            f" rss_after_trim={metrics.get('rss_bytes_after_trim')}"
            if trimmed is not None
            else ""
        ),
    )
    if tracemalloc_top > 0:
        _log_tracemalloc_top(tracemalloc_top, metrics)
    return metrics


def _log_tracemalloc_top(top_n: int, metrics: dict[str, Any]) -> None:
    try:
        import tracemalloc

        if not tracemalloc.is_tracing():
            return
        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.statistics("lineno")[:top_n]
        top = [f"{stat.traceback[0]}={stat.size}" for stat in stats]
        metrics["tracemalloc_top"] = top
        LOGGER.info("memtrace tracemalloc_top=%s", top)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("tracemalloc sample skipped: %s", type(exc).__name__)


def start_memory_sampler(
    stop_event: threading.Event | None = None,
) -> threading.Event | None:
    """Start the opt-in memory sampler daemon thread.

    Returns the stop event when started, or ``None`` when disabled (interval
    unset / <= 0). Enable with ``API_MEMTRACE_INTERVAL_SECONDS=<seconds>``;
    optional ``API_MEMTRACE_TRACEMALLOC=1`` (starts tracemalloc + logs top-N),
    ``API_MEMTRACE_TOPN=<n>``, ``API_MALLOC_TRIM=1`` (return arenas after each
    sample).
    """
    interval = _env_float("API_MEMTRACE_INTERVAL_SECONDS", 0.0)
    if interval <= 0:
        return None
    interval = max(_MIN_INTERVAL_SECONDS, interval)
    top_n = _env_int("API_MEMTRACE_TOPN", _DEFAULT_TOPN, minimum=0, maximum=50)
    trim = _env_flag("API_MALLOC_TRIM")
    trace = _env_flag("API_MEMTRACE_TRACEMALLOC")
    if trace:
        try:
            import tracemalloc

            if not tracemalloc.is_tracing():
                tracemalloc.start(_env_int("API_MEMTRACE_FRAMES", 1, minimum=1, maximum=30))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("tracemalloc start failed: %s", type(exc).__name__)
            trace = False

    event = stop_event or threading.Event()

    def _loop() -> None:
        LOGGER.info(
            "memtrace sampler started interval=%.1fs tracemalloc=%s malloc_trim=%s topn=%d",
            interval,
            trace,
            trim,
            top_n if trace else 0,
        )
        # Wait first so a fast-crashing process does not spam a sample on boot.
        while not event.wait(timeout=interval):
            try:
                sample_once(tracemalloc_top=top_n if trace else 0, trim=trim)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("memtrace sample failed: %s", type(exc).__name__)
        LOGGER.info("memtrace sampler stopped")

    thread = threading.Thread(target=_loop, name="api-memtrace", daemon=True)
    thread.start()
    return event
