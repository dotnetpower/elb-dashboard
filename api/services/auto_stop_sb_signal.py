"""Gated, TTL-cached Service Bus request-queue keep-alive signal for auto-stop.

Responsibility: Produce the single "should a Running AKS cluster stay up
    because the deployment-wide Service Bus request queue still holds undrained
    work?" signal, shared by the auto-stop beat driver
    (``api.tasks.azure.idle_autostop``) and the auto-stop status route
    (``api.routes.aks.autostop``). Composes the auto-stop env gate
    (``AKS_AUTOSTOP_RESPECT_SB_QUEUE``), the Service Bus enable gate, and the
    best-effort ``service_bus.pending_request_count`` data-plane read, with an
    optional short TTL cache so a high-frequency status poll does not turn into
    one Service Bus admin call per poll per cluster.
Edit boundaries: Reusable cross-layer signal only — no HTTP shaping, no AKS SDK
    call, no auto-stop decision logic (that lives in ``auto_stop_evaluator``).
    The data-plane read itself lives in ``api.services.service_bus``; this
    module only gates and caches it.
Key entry points: ``pending_queue_signal``.
Risky contracts: ``pending_queue_signal`` MUST NEVER raise — any failure (SB
    disabled, missing claims, admin call error) degrades to ``None`` so an
    unreadable queue can never strand a cluster running forever. ``None`` is an
    additive-protection no-op for the evaluator. The TTL cache is
    deployment-global (the request queue is a single deployment-wide entity),
    so one cached value covers every cluster and dashboard; a non-Running
    cluster never consults the cache. ``ttl_seconds <= 0`` disables the cache
    (the beat driver uses this to keep the act-path stop decision real-time).
Validation: ``uv run pytest -q api/tests/test_auto_stop_sb_signal.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from api.services import service_bus, service_bus_pref

LOGGER = logging.getLogger(__name__)

_RESPECT_QUEUE_ENV = "AKS_AUTOSTOP_RESPECT_SB_QUEUE"
_OFF_VALUES = {"0", "false", "no"}
_DEFAULT_TTL_SECONDS = 5.0


@dataclass
class _CacheEntry:
    value: int | None
    expires_at: float


_CACHE: _CacheEntry | None = None
_CACHE_LOCK = threading.Lock()


def _respect_queue_enabled() -> bool:
    """``AKS_AUTOSTOP_RESPECT_SB_QUEUE`` gate (default ON, env-disable)."""
    return os.environ.get(_RESPECT_QUEUE_ENV, "true").strip().lower() not in _OFF_VALUES


def _read_pending() -> int | None:
    """Gated, best-effort active request-queue depth — never raises.

    Returns ``None`` when the env gate is off, Service Bus is disabled, or the
    admin read fails; otherwise the active (deliverable) message count.
    """
    if not _respect_queue_enabled():
        return None
    try:
        if not service_bus_pref.service_bus_enabled():
            return None
        return service_bus.pending_request_count(service_bus_pref.get_service_bus_config())
    except Exception as exc:  # best-effort additive signal -- never fail the caller
        LOGGER.debug("sb pending signal read failed: %s", exc)
        return None


def pending_queue_signal(
    power_state: str, *, ttl_seconds: float = _DEFAULT_TTL_SECONDS
) -> int | None:
    """Active request-queue depth that should keep a Running cluster alive.

    Only meaningful for a ``Running`` cluster: a stopped/starting cluster is
    already kept (or being acted on) by the power-state gate upstream, and
    auto-START on queue arrival is intentionally out of scope — this only
    prevents an idle stop while queued work waits to be drained. Returns the
    active (deliverable) message count, or ``None`` when the signal is
    unavailable/disabled so the evaluator degrades to the job-count decision.

    ``ttl_seconds`` caps how stale a cached read may be. The request queue is a
    single deployment-wide entity, so the cache is global: one Service Bus
    admin call serves every cluster and every concurrent status poll within the
    window. Pass ``ttl_seconds <= 0`` to bypass the cache (the auto-stop beat
    driver does this so the act-path stop decision reads the live queue).
    """
    if power_state != "Running":
        return None
    if ttl_seconds <= 0:
        return _read_pending()

    global _CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE
        if cached is not None and cached.expires_at > now:
            return cached.value

    value = _read_pending()

    with _CACHE_LOCK:
        _CACHE = _CacheEntry(value=value, expires_at=time.monotonic() + ttl_seconds)
    return value


def _reset_cache_for_tests() -> None:
    """Clear the module-global cache so unit tests do not leak state."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def read_request_queue_depth() -> int | None:
    """Active request-queue depth regardless of cluster power state.

    This is the **queue-arrival auto-START** read, the deliberate counterpart to
    :func:`pending_queue_signal` (which is Running-only keep-alive and returns
    ``None`` for a stopped cluster). The auto-start evaluator needs the depth for
    a STOPPED cluster so a queued submission can trigger a start, so this read is
    power-state agnostic.

    Gated ONLY by the Service Bus enable state — NOT by
    ``AKS_AUTOSTOP_RESPECT_SB_QUEUE`` (that env is the stop-side keep-alive knob;
    the start side has its own gate in ``queue_autostart``). Never raises: any
    failure (SB disabled, missing claims, admin call error) degrades to ``None``
    so an unreadable queue can never trigger a cost-bearing start.
    """
    try:
        if not service_bus_pref.service_bus_enabled():
            return None
        return service_bus.pending_request_count(service_bus_pref.get_service_bus_config())
    except Exception as exc:  # never trigger a start on a failed read
        LOGGER.debug("autostart pending depth read failed: %s", exc)
        return None


__all__ = ["pending_queue_signal"]
