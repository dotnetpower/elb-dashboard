"""Stateless helpers for the ``elb-openapi`` AKS deploy task.

Responsibility: Provide small pure helpers used by the OpenAPI deploy pipeline
    (cluster node-count derivation, ISO timestamp, Celery progress update).
Edit boundaries: No side effects beyond the Celery state backend. Heavy work
    (RBAC writes, kubectl apply, IP polling) belongs in the dedicated sibling modules.
Key entry points: `blast_node_count`, `now_iso`, `record_progress`.
Risky contracts: `record_progress` must remain best-effort — never raise — so a
    Celery state backend hiccup cannot fail the surrounding deploy task.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)


def blast_node_count(cluster: Any) -> int:
    """Return the desired blastpool node count, bounded for core_nt partitions."""

    pools = getattr(cluster, "agent_pool_profiles", None) or []
    for pool in pools:
        labels = getattr(pool, "node_labels", None) or {}
        if labels.get("workload") == "blast" or getattr(pool, "name", "") == "blastpool":
            count = getattr(pool, "count", None) or getattr(pool, "node_count", None) or 0
            if count:
                return max(1, min(int(count), 10))
    return 10


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_progress(self: Any, phase: str, **extra: Any) -> None:
    """Push a Celery PROGRESS update so the SPA can render the phase.

    The status route maps any non-terminal Celery state plus the
    ``custom_status`` ``phase`` field into the orchestrator-style envelope
    the SPA was originally written against (Pending / Running / Completed
    / Failed / Terminated).
    """

    LOGGER.info("openapi_deploy phase=%s extra=%s", phase, extra)
    try:
        self.update_state(state="PROGRESS", meta={"phase": phase, **extra})
    except Exception as exc:
        # State backend is best-effort; never let a backend hiccup fail the task.
        LOGGER.debug("update_state failed for phase=%s: %s", phase, exc)
