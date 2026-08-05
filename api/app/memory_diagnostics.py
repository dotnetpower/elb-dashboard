"""Memory diagnostics sampler + arena reclaimer for the api sidecar.

Two independent pieces with different defaults:

* ``start_arena_reclaimer`` — **default-ON production mitigation.** A periodic
  ``malloc_trim(0)`` that hands freed glibc arenas back to the OS. The api
  sidecar reads large transient buffers into memory (blob downloads, then
  XML/JSON parsing); glibc retains those arenas, so RSS ratchets up to the
  container limit and the sidecar is SIGKILL'd (exit 137) while the live heap is
  a fraction of it.
* ``start_memory_sampler`` — **default-OFF diagnostics.** Periodically samples
  and logs process memory (RSS + GC stats + optional ``tracemalloc`` top-N) so a
  *suspected* leak can be confirmed as unbounded growth vs a bounded plateau.
  Zero runtime cost when ``API_MEMTRACE_INTERVAL_SECONDS`` is unset / <= 0.

Responsibility: Own the api sidecar's memory daemon threads (arena reclaimer +
optional sampler) plus the shared ``malloc_trim`` / RSS helpers.
Edit boundaries: Diagnostics and allocator mitigation only. Stay stdlib-only; do
not import route/service business logic. The sampler must be a no-op when
disabled.
Key entry points: ``start_arena_reclaimer``, ``start_memory_sampler``,
``read_rss_bytes``, ``sample_once``, ``malloc_trim``.
Risky contracts: Both loops must NEVER raise out (a diagnostics/mitigation
thread must not crash the sidecar). ``tracemalloc`` adds allocation-tracking
overhead so it is gated separately from the base RSS/GC sample. The reclaimer
must not start when ``malloc_trim`` is unavailable (musl), or it would spin
forever doing nothing.
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


# --------------------------------------------------------------------------- #
# Always-on arena reclaimer (production mitigation, not diagnostics)
# --------------------------------------------------------------------------- #

# Default cadence for the standalone reclaimer. 60 s matches the interval that
# measurably held RSS flat in production without showing up in the CPU profile
# (one `malloc_trim(0)` is microseconds of work).
_DEFAULT_TRIM_INTERVAL_SECONDS = 60.0
_MIN_TRIM_INTERVAL_SECONDS = 10.0


def start_arena_reclaimer(
    stop_event: threading.Event | None = None,
) -> threading.Event | None:
    """Start the periodic ``malloc_trim`` thread that keeps api RSS bounded.

    Unlike :func:`start_memory_sampler` this is a **production mitigation**, not
    a diagnostic, so it is default-ON and carries none of the sampler's cost (no
    ``tracemalloc``, no per-minute log line, no ``gc.get_objects()`` walk).

    Why it exists: the api sidecar serves large transient buffers (blob
    downloads read into memory, then XML/JSON parsed). glibc keeps those freed
    arenas instead of returning them to the OS, so RSS ratchets upward until the
    container hits its memory limit and is SIGKILL'd (exit 137) even though the
    live heap is a fraction of it. Measured in production: ``malloc_trim(0)``
    reclaimed 221-283 MiB (40-47% of RSS) on every single sample, and RSS went
    from "climbs to 2 GiB then OOM" to a flat 320-380 MiB live.

    Disable with ``API_ARENA_RECLAIM_INTERVAL_SECONDS=0`` (kill switch); tune the
    cadence with the same variable. Returns the stop event, or ``None`` when
    disabled or when the platform has no usable ``malloc_trim``.
    """
    interval = _env_float(
        "API_ARENA_RECLAIM_INTERVAL_SECONDS", _DEFAULT_TRIM_INTERVAL_SECONDS
    )
    if interval <= 0:
        LOGGER.info("arena reclaimer disabled by API_ARENA_RECLAIM_INTERVAL_SECONDS")
        return None
    interval = max(_MIN_TRIM_INTERVAL_SECONDS, interval)
    # Probe once up front: on musl (or any libc without malloc_trim) the thread
    # would spin forever doing nothing, so do not start it at all.
    if not malloc_trim():
        LOGGER.info("arena reclaimer not started: malloc_trim unavailable")
        return None

    event = stop_event or threading.Event()

    def _loop() -> None:
        LOGGER.info("arena reclaimer started interval=%.1fs", interval)
        while not event.wait(timeout=interval):
            try:
                malloc_trim()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.debug("arena reclaim skipped: %s", type(exc).__name__)
        LOGGER.info("arena reclaimer stopped")

    thread = threading.Thread(target=_loop, name="api-arena-reclaim", daemon=True)
    thread.start()
    return event
