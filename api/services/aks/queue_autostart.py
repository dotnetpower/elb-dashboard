"""Queue-arrival AKS auto-start decision + cooldown lease (inverse of idle auto-stop).

Responsibility: Decide whether a STOPPED AKS cluster should be auto-started
    because the deployment-wide Service Bus request queue holds undrained work,
    and rate-limit that start with a per-cluster cooldown lease. Idle auto-stop's
    Service Bus keep-alive only PREVENTS a stop while work waits
    (``auto_stop_sb_signal.pending_queue_signal``); this is the deliberately
    separate START side. Default-OFF because a start spins up billable compute.
Edit boundaries: decision + lease only. NO AKS SDK call (the beat task enqueues
    ``start_aks``), NO queue read (that is
    ``auto_stop_sb_signal.read_request_queue_depth``), NO HTTP shaping.
Key entry points: ``queue_autostart_enabled``, ``should_autostart``,
    ``acquire_autostart_lease``, ``release_autostart_lease``,
    ``request_autostart_evaluation``.
Risky contracts: ``should_autostart`` returns True ONLY for an exactly-``Stopped``
    cluster (never ``Stopping`` / ``Starting`` / ``Running`` / blank-unknown) with
    a positive pending depth and the gate on, so a transient power-state blank or
    an in-flight start can never double-trigger. ``acquire_autostart_lease`` is
    FAIL-CLOSED: a Redis error returns False (no start) — a missed start is cheap
    (the next beat tick retries) but a spurious start costs money. The lease TTL
    doubles as the single-flight guard so two overlapping beat ticks / two
    workers cannot both start the same cluster; it is never released early so a
    finished-then-restopped cluster only restarts after the cooldown.
Validation: ``uv run pytest -q api/tests/test_queue_autostart.py``.
"""

from __future__ import annotations

import logging
import os
import uuid

LOGGER = logging.getLogger(__name__)

_GATE_ENV = "SERVICEBUS_QUEUE_AUTOSTART"
_COOLDOWN_ENV = "SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS"
_LEASE_KEY_PREFIX = "aks:queue-autostart"
_ON_VALUES = {"1", "true", "yes"}


def queue_autostart_enabled() -> bool:
    """Default-OFF gate (charter §12a Rule 4): a start spins up billable compute."""
    return os.environ.get(_GATE_ENV, "").strip().lower() in _ON_VALUES


def _cooldown_seconds() -> int:
    """Cooldown / lease TTL, floored at 60s, fail-safe on a bad value."""
    raw = os.environ.get(_COOLDOWN_ENV, "600")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 600
    return max(60, value)


def should_autostart(power_state: str, pending_depth: int | None) -> bool:
    """True when a STOPPED cluster has undrained queued work and the gate is on.

    Strict on ``power_state``: only an exactly-``Stopped`` cluster qualifies. A
    blank (ARM unreachable), ``Stopping``, ``Starting`` or ``Running`` state
    returns False so a transient read or an in-flight start never double-triggers
    a (cost-bearing) start.
    """
    if not queue_autostart_enabled():
        return False
    if power_state != "Stopped":
        return False
    return bool(pending_depth and pending_depth > 0)


def _lease_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    return f"{_LEASE_KEY_PREFIX}:{subscription_id}:{resource_group}:{cluster_name}"


def acquire_autostart_lease(
    subscription_id: str, resource_group: str, cluster_name: str
) -> bool:
    """Take a per-cluster cooldown lease before enqueuing a start.

    Returns True (proceed to start) only if no lease is currently held for this
    cluster; the lease TTL (``SERVICEBUS_QUEUE_AUTOSTART_COOLDOWN_SECONDS``,
    default 600s, floored 60s) is BOTH the cooldown AND the single-flight guard,
    so two overlapping beat ticks or two workers cannot both start the cluster.
    FAIL-CLOSED: any Redis error returns False (no start). The lease is never
    released early; it expires so a finished-then-restopped cluster can be
    restarted only after the cooldown.
    """
    try:
        from api.services.redis_clients import get_broker_redis_client

        client = get_broker_redis_client(socket_timeout=2)
        key = _lease_key(subscription_id, resource_group, cluster_name)
        return bool(client.set(key, uuid.uuid4().hex, nx=True, ex=_cooldown_seconds()))
    except Exception as exc:
        LOGGER.debug("queue-autostart lease acquire failed (fail-closed, no start): %s", exc)
        return False


def release_autostart_lease(
    subscription_id: str, resource_group: str, cluster_name: str
) -> None:
    """Release a freshly-acquired lease when the start enqueue did NOT happen.

    ``acquire_autostart_lease`` reserves the cooldown the moment it returns True,
    on the assumption the caller will enqueue ``start_aks``. If that enqueue then
    raises, the lease would otherwise block any retry for the full cooldown even
    though no start was actually queued. Calling this rolls the reservation back
    so the next beat tick can re-attempt the start immediately. Best-effort: a
    Redis error leaves the lease to expire via its TTL (the cooldown), which is
    the safe direction — at worst the start is delayed, never duplicated.
    """
    try:
        from api.services.redis_clients import get_broker_redis_client

        client = get_broker_redis_client(socket_timeout=2)
        client.delete(_lease_key(subscription_id, resource_group, cluster_name))
    except Exception as exc:
        LOGGER.debug("queue-autostart lease release failed (will expire via TTL): %s", exc)


def request_autostart_evaluation(reason: str = "") -> None:
    """Trigger an immediate idle/auto-start evaluation the moment a request is
    enqueued — the event-driven counterpart to the 5-minute beat tick.

    Without this, a queued request waits out the next scheduled
    ``evaluate_idle_clusters`` run (up to ``CELERY_BEAT_AKS_IDLE_AUTOSTOP_SECONDS``,
    default 300s) before a Stopped cluster is started. Kicking the same task here
    starts the cluster within seconds of the message landing. Gated by
    ``queue_autostart_enabled()`` (a no-op when the feature is off, so the legacy
    poll-only behaviour is unchanged). Best-effort: a broker hiccup is swallowed
    (the scheduled tick remains the fallback) so this never raises into the
    producer. The single-flight cooldown lease in ``evaluate_idle_clusters``
    de-dupes a burst of enqueues into at most one start per cooldown.
    """
    if not queue_autostart_enabled():
        return
    try:
        from api.tasks.azure.idle_autostop import evaluate_idle_clusters

        evaluate_idle_clusters.delay()  # type: ignore[attr-defined]
        LOGGER.debug("queue-autostart eval triggered on enqueue (reason=%s)", reason or "")
    except Exception as exc:
        LOGGER.debug("queue-autostart eval trigger skipped: %s", exc)

