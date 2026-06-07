"""`reconcile_auto_warmup` Celery task — reconcile Auto warm preferences against AKS.

Responsibility: Beat-scheduled reconciler that translates persisted Auto warm preferences
    into per-database warmup enqueues whenever the target AKS cluster becomes ready.
Edit boundaries: Delegate the actual reconciliation logic to
    `api.services.auto_warmup_reconcile.reconcile_auto_warmup_preferences`. Keep this
    module a thin Celery wrapper.
Key entry points: `reconcile_auto_warmup` (Celery task
    `api.tasks.storage.reconcile_auto_warmup`).
Risky contracts: Task name must stay `api.tasks.storage.reconcile_auto_warmup` because
    beat schedules and the AKS provision task reference it by string. The in-flight
    guard (`_autowarmup_inflight_acquire`) is monkeypatched by tests via this module.
Validation: `uv run pytest -q api/tests/test_auto_warmup.py`.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

import api.tasks.storage as _facade


@shared_task(name="api.tasks.storage.reconcile_auto_warmup", bind=True)
def reconcile_auto_warmup(
    self: Any,
    *,
    preference: dict[str, Any] | None = None,
    force: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Reconcile server-side Auto warm preferences against AKS readiness.

    Side effects: reads AKS/Kubernetes/Storage state, updates the persisted
    Auto warm preference readiness marker, and enqueues node-local warmup tasks
    for configured DBs when a cluster becomes workload-ready.
    """

    from api.celery_app import celery_app
    from api.services.auto_warmup_reconcile import reconcile_auto_warmup_preferences

    # Overlap guard for the beat (full-list) path: a slow reconcile pass can
    # outlive the 120 s beat interval, so two ticks could run concurrently and
    # double-process every preference (extra K8s/Storage reads, racing CAS
    # writes). A short Redis lock keeps only one full pass in flight; the lock
    # auto-expires so a crashed worker cannot wedge reconciliation. The
    # single-preference path (force re-warm from `start_aks`, tests) is never
    # gated — it is targeted and must always run promptly.
    lock = None
    if preference is None:
        from api.services.auto_warmup_reconcile import autowarmup_inflight_redis

        client = autowarmup_inflight_redis()
        if client is not None:
            try:
                if not client.set("autowarmup:reconcile:lock", "1", nx=True, ex=110):
                    return {"status": "skipped", "reason": "reconcile_already_running"}
                lock = client
            except Exception:  # pragma: no cover - defensive
                lock = None

    try:
        return reconcile_auto_warmup_preferences(
            credential=_facade.get_credential(),
            send_task=celery_app.send_task,
            preference=preference,
            force=force,
            limit=limit,
            inflight_acquire=_facade._autowarmup_inflight_acquire,
        )
    finally:
        if lock is not None:
            try:
                lock.delete("autowarmup:reconcile:lock")
            except Exception:  # noqa: S110 - lock auto-expires; release races are not fatal
                pass
