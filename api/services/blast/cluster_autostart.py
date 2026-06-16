"""Wake-on-request auto-start for the Service Bus BLAST drain pipeline.

When a BLAST request lands on the Service Bus request queue while the configured
AKS cluster is stopped, the drain cannot run (the OpenAPI plane is down with the
cluster). This module decides — once per drain pass — whether to (a) proceed
draining (cluster ready), (b) hold the drain and kick an idempotent
``start_aks`` (cluster stopped AND work is pending), or (c) hold the drain
silently (cluster mid start/warmup). It reuses the proven
``evaluate_ensure_running`` readiness brain and the existing ``start_aks`` task,
so it never duplicates power-state logic or the start side effect.

Responsibility: One decision — ``evaluate_for_drain(cfg)`` — plus a best-effort
    Redis debounce so repeated 30 s ticks cannot flood ``start_aks``. No power
    state parsing (delegated to ``evaluate_ensure_running``), no Service Bus
    receive, no HTTP shaping.
Edit boundaries: Reusable domain logic only. Service Bus access goes through
    ``api.services.service_bus``; the start side effect is the existing
    ``api.tasks.azure.start_aks`` Celery task enqueued with ``.delay``. Keep the
    decision pure + bounded; never raise into the drain loop.
Key entry points: ``AutostartDecision``, ``autostart_enabled``,
    ``evaluate_for_drain``.
Risky contracts: A start is enqueued ONLY for a fully ``stopped`` cluster with
    ``start_recommended`` AND at least one peeked request message — so an empty
    queue can never silently rack up cluster-start cost, and an in-flight
    stop/start LRO is never raced (``start_recommended`` is False while
    Stopping/Starting). The debounce is best-effort: a Redis outage degrades to
    "allow the kick" because ``start_aks`` is itself idempotent. Any evaluation
    error degrades to ``proceed_with_drain=True`` (best-effort: let the existing
    drain try) rather than blocking the queue forever.
Validation: ``uv run pytest -q api/tests/test_cluster_autostart.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from api.services.service_bus_pref import ServiceBusConfig

LOGGER = logging.getLogger(__name__)

# One start kick per cluster per window — repeated drain ticks (beat = 30 s,
# resident loop ~5 s) must not enqueue a fresh start_aks every tick while the
# cluster is coming up. start_aks is idempotent, but the debounce avoids log /
# task-queue noise. Best-effort (Redis-backed).
_DEBOUNCE_TTL_SECONDS = 120
_DEBOUNCE_KEY = "elb:sb:autostart:{cluster}"


@dataclass
class AutostartDecision:
    """Outcome of one pre-drain auto-start evaluation.

    ``proceed_with_drain`` is the load-bearing field the drain caller branches
    on: True means run the normal drain pass; False means hold (messages stay in
    the queue until a later tick). ``status`` mirrors the ensure-running
    vocabulary plus a few local sentinels (``disabled`` / ``no_cluster`` /
    ``no_pending`` / ``error``) for observability.
    """

    proceed_with_drain: bool
    started: bool
    status: str
    start_task_id: str | None = None
    reason: str = ""


def autostart_enabled(cfg: ServiceBusConfig) -> bool:
    """True when the operator opted into wake-on-request auto-start."""
    return bool(getattr(cfg, "autostart_cluster_enabled", False))


def _cluster_context_complete(cfg: ServiceBusConfig) -> bool:
    return bool(cfg.subscription_id and cfg.resource_group and cfg.cluster_name)


def _has_pending_request(cfg: ServiceBusConfig) -> bool:
    """Non-destructively peek the request queue for at least one message.

    Uses the data-plane peek (needs only ``Listen``), so it works even when the
    credential lacks the ``Manage`` claim ``entity_counts`` requires. A peek
    failure degrades to ``False`` (do not start on an unverifiable queue) — the
    next tick re-evaluates, so a transient peek error never starts a cluster on
    a possibly-empty queue.
    """
    try:
        from api.services import service_bus

        return len(service_bus.peek_requests(cfg, max_count=1)) > 0
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("autostart peek failed: %s", type(exc).__name__)
        return False


def _debounce_ok(cluster_name: str) -> bool:
    """True when no recent start kick was recorded for this cluster.

    Best-effort SET NX with TTL on the shared OPS Redis. A Redis outage degrades
    to True (allow the kick) because ``start_aks`` is idempotent — better an
    extra idempotent enqueue than never starting because the debounce store is
    down.
    """
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client()
        if client is None:
            return True
        key = _DEBOUNCE_KEY.format(cluster=cluster_name)
        return bool(client.set(key, "1", nx=True, ex=_DEBOUNCE_TTL_SECONDS))
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("autostart debounce check failed: %s", type(exc).__name__)
        return True


def evaluate_for_drain(cfg: ServiceBusConfig) -> AutostartDecision:
    """Decide whether the current drain pass may proceed, starting AKS if needed.

    Returns ``proceed_with_drain=True`` only when the cluster is ``ready`` (or
    auto-start is not applicable, so the existing best-effort drain runs). For a
    stopped cluster with pending work it enqueues ``start_aks`` (debounced) and
    holds the drain; for a mid start/warmup cluster it holds the drain without
    re-kicking. Never raises.
    """
    if not autostart_enabled(cfg):
        return AutostartDecision(True, False, "disabled")
    if not _cluster_context_complete(cfg):
        return AutostartDecision(
            True, False, "no_cluster", reason="cluster routing context incomplete"
        )

    try:
        from api.services import get_credential
        from api.services.aks.ensure_running import evaluate_ensure_running

        result = evaluate_ensure_running(
            get_credential(),
            subscription_id=cfg.subscription_id,
            resource_group=cfg.resource_group,
            cluster_name=cfg.cluster_name,
        )
    except Exception as exc:
        LOGGER.warning("autostart readiness evaluation failed: %s", type(exc).__name__)
        # Cannot determine state — do NOT block the queue. Let the existing drain
        # attempt run (it degrades on its own if the cluster is truly down).
        return AutostartDecision(True, False, "error", reason=type(exc).__name__)

    status = str(result.get("status") or "unknown")
    if status == "ready":
        return AutostartDecision(True, False, "ready")

    # not_found / unknown: auto-start cannot help (no cluster to start, or state
    # indeterminate). Fall back to the existing best-effort drain rather than
    # holding the queue forever.
    if status in {"not_found", "unknown"}:
        return AutostartDecision(True, False, status, reason=str(result.get("reason") or ""))

    # starting / warming: the cluster is already coming up. Hold the drain (the
    # messages wait safely in the queue) but do not re-kick a start.
    if status in {"starting", "warming"}:
        return AutostartDecision(False, False, status, reason=str(result.get("reason") or ""))

    # stopped: start only when there is actual pending work AND a start is
    # recommended (never while a stop/start LRO is in flight).
    if status == "stopped" and result.get("start_recommended"):
        if not _has_pending_request(cfg):
            return AutostartDecision(
                False, False, "no_pending", reason="cluster stopped; request queue empty"
            )
        return _kick_start(cfg)

    # stopped but start not recommended (mid-stop) — hold without kicking.
    return AutostartDecision(False, False, status, reason=str(result.get("reason") or ""))


def _kick_start(cfg: ServiceBusConfig) -> AutostartDecision:
    """Enqueue an idempotent ``start_aks`` for the configured cluster (debounced)."""
    if not _debounce_ok(cfg.cluster_name):
        return AutostartDecision(
            False, False, "starting", reason="start already kicked within debounce window"
        )
    try:
        from api.tasks.azure import start_aks

        async_result = start_aks.delay(
            subscription_id=cfg.subscription_id,
            resource_group=cfg.resource_group,
            cluster_name=cfg.cluster_name,
        )
    except Exception as exc:
        LOGGER.warning("autostart start_aks enqueue failed: %s", type(exc).__name__)
        # Could not enqueue — hold the drain; a later tick retries.
        return AutostartDecision(
            False, False, "stopped", reason=f"enqueue_failed:{type(exc).__name__}"
        )

    task_id = getattr(async_result, "id", None)
    LOGGER.info(
        "service bus autostart: enqueued start_aks cluster=%s rg=%s task=%s",
        cfg.cluster_name,
        cfg.resource_group,
        task_id,
    )
    return AutostartDecision(
        False, True, "stopped", start_task_id=task_id, reason="cluster start enqueued"
    )
