"""Beat and targeted Auto oracle reconciliation task.

Responsibility: Serialize full-fleet Auto oracle reconcile passes and delegate
    bounded preference decisions to `api.services.auto_oracle_reconcile`.
Edit boundaries: Thin Celery wrapper and ephemeral overlap lock only; all
    eligibility and dispatch logic lives in the service.
Key entry points: `reconcile_auto_oracle` (Celery task
    `api.tasks.storage.reconcile_auto_oracle`).
Risky contracts: Full and targeted passes share one token-owned expiring Redis
    lock on the reconcile queue; durable oracle claims remain execution authority.
Validation: `uv run pytest -q api/tests/test_auto_oracle_reconcile.py`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)


@shared_task(
    name="api.tasks.storage.reconcile_auto_oracle",
    bind=True,
    soft_time_limit=100,
    time_limit=110,
)
def reconcile_auto_oracle(
    self: Any,
    *,
    preference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile enabled Auto oracle preferences.

    Side effects: reads preferences/RBAC/Storage/AKS state and may enqueue
    generation-bound order-oracle build tasks.
    """
    del self
    from api.celery_app import celery_app
    from api.services import get_credential
    from api.services.auto_oracle_reconcile import (
        reconcile_auto_oracle_preferences,
    )

    lock_key = "autooracle:reconcile:lock"
    lock_token = uuid.uuid4().hex
    try:
        from api.services.redis_clients import get_ops_redis_client

        lock = get_ops_redis_client(socket_timeout=1.5)
        if not lock.set(lock_key, lock_token, nx=True, ex=110):
            return {"status": "skipped", "reason": "reconcile_already_running"}
    except Exception as exc:
        LOGGER.warning(
            "auto oracle reconcile lock unavailable reason=%s",
            type(exc).__name__,
        )
        return {"status": "skipped", "reason": "reconcile_lock_unavailable"}
    try:
        return reconcile_auto_oracle_preferences(
            credential=get_credential(),
            send_task=celery_app.send_task,
            preference=preference,
        )
    finally:
        try:
            lock.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_token,
            )
        except Exception as exc:
            LOGGER.debug(
                "auto oracle reconcile lock release skipped reason=%s",
                type(exc).__name__,
            )
