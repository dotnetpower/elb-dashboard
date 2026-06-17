"""Per-cluster circuit breaker for unreachable AKS API servers.

Responsibility: Suppress repeated ARM kubeconfig / Kubernetes API calls to a
cluster that is stopped or deleted, so a long-down cluster does not record one
``requests.exceptions.ConnectionError`` (``NameResolutionError``) per dashboard
poll in App Insights. The breaker is keyed by ``(subscription, resource_group,
cluster)``; a single successful call closes it.
Edit boundaries: Pure in-memory breaker state + env knobs. No Azure SDK, no
Kubernetes calls, no ``requests`` import — `api.services.k8s.client` wires the
check/record hooks into the session + credential choke points.
Key entry points: ``cluster_breaker_check``, ``cluster_breaker_record_failure``,
``cluster_breaker_record_success``, ``reset_cluster_breaker``,
``ClusterApiUnreachable``.
Risky contracts: ``cluster_breaker_check`` raises ``ClusterApiUnreachable`` (a
builtin ``ConnectionError`` subclass) when the breaker is open — callers must
keep catching it with their existing broad ``except Exception`` graceful
handlers so a down cluster degrades to empty/None, never a 500.
Validation: `uv run pytest -q api/tests/test_cluster_breaker.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

# Trip the breaker after this many consecutive connect/DNS failures (each k8s
# session GET is already retried once by urllib3, so two failures past that
# means a real outage, not a coredns blip). Keep the cooldown short so a
# briefly-stopped cluster that comes back is re-probed quickly.
_DEFAULT_FAIL_THRESHOLD = 2
_DEFAULT_COOLDOWN_SECONDS = 120.0


@dataclass
class _BreakerState:
    """Mutable per-cluster failure accumulator.

    ``fail_count`` rises on each connect failure; once it reaches the
    threshold ``open_until`` is set and the breaker rejects calls until that
    monotonic deadline passes, after which the entry is dropped (optimistic
    close) and the next real call decides whether to re-trip.
    """

    fail_count: int = 0
    open_until: float = 0.0


class ClusterApiUnreachable(ConnectionError):
    """Raised by ``cluster_breaker_check`` while a cluster's breaker is open.

    Subclasses the builtin ``ConnectionError`` (an ``OSError``) so the existing
    broad ``except Exception`` graceful handlers in the k8s helpers catch it
    exactly like the real ``requests`` connect error it stands in for — but it
    is raised by our code *before* any network call, so the OpenTelemetry
    ``requests`` instrumentor never records it as an App Insights exception.
    """


_BREAKER: dict[tuple[str, str, str], _BreakerState] = {}
_BREAKER_LOCK = threading.Lock()


def _fail_threshold() -> int:
    raw = os.environ.get("K8S_CLUSTER_BREAKER_THRESHOLD", "")
    if raw:
        try:
            return max(1, min(int(raw), 100))
        except ValueError:
            return _DEFAULT_FAIL_THRESHOLD
    return _DEFAULT_FAIL_THRESHOLD


def _cooldown_seconds() -> float:
    raw = os.environ.get("K8S_CLUSTER_BREAKER_COOLDOWN_SECONDS", "")
    if raw:
        try:
            return max(1.0, min(float(raw), 3600.0))
        except ValueError:
            return _DEFAULT_COOLDOWN_SECONDS
    return _DEFAULT_COOLDOWN_SECONDS


def _disabled() -> bool:
    return os.environ.get("K8S_CLUSTER_BREAKER_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def cluster_breaker_key(
    subscription_id: str, resource_group: str, cluster_name: str
) -> tuple[str, str, str]:
    """Normalised breaker key. ``admin`` is intentionally excluded — a down
    cluster fails the user and admin credential paths alike."""

    return (subscription_id, resource_group, cluster_name)


def cluster_breaker_check(key: tuple[str, str, str]) -> None:
    """Raise :class:`ClusterApiUnreachable` if ``key``'s breaker is open.

    When the cooldown has elapsed the entry is dropped (optimistic close) so
    the caller's real ARM/Kubernetes call can re-probe the cluster. No-op when
    the breaker is disabled or the cluster has no recorded failures.
    """

    if _disabled():
        return
    now = time.monotonic()
    with _BREAKER_LOCK:
        state = _BREAKER.get(key)
        if state is None or not state.open_until:
            return
        if now >= state.open_until:
            # Cooldown elapsed — drop the entry and let the next real call
            # decide whether the cluster is reachable again.
            _BREAKER.pop(key, None)
            return
        open_for = state.open_until
    raise ClusterApiUnreachable(
        f"AKS cluster {key[2]!r} API is unreachable; circuit breaker open "
        f"for {max(0.0, open_for - now):.0f}s more"
    )


def cluster_breaker_record_failure(key: tuple[str, str, str]) -> None:
    """Record a connect/DNS failure; trip the breaker at the threshold."""

    if _disabled():
        return
    threshold = _fail_threshold()
    with _BREAKER_LOCK:
        state = _BREAKER.setdefault(key, _BreakerState())
        state.fail_count += 1
        tripped = state.fail_count >= threshold and not state.open_until
        if state.fail_count >= threshold:
            state.open_until = time.monotonic() + _cooldown_seconds()
    if tripped:
        # One log per trip (not per poll) so a down cluster is visible without
        # the flood the breaker exists to prevent.
        LOGGER.info(
            "cluster_breaker: opened for cluster %r after %d connect failures",
            key[2],
            threshold,
        )


def cluster_breaker_record_success(key: tuple[str, str, str]) -> None:
    """Clear any breaker state for ``key`` after a successful call."""

    if _disabled():
        return
    with _BREAKER_LOCK:
        _BREAKER.pop(key, None)


def reset_cluster_breaker() -> None:
    """Drop all breaker state. Test-only / cache-reset hook."""

    with _BREAKER_LOCK:
        _BREAKER.clear()
