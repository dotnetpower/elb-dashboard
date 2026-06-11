"""Service Bus integration task package (thin facade).

Responsibility: Aggregate and re-export the Service Bus Celery tasks so Celery
    auto-discovers them by their stable ``api.tasks.servicebus.*`` names and
    callers/tests can import them from this package.
Edit boundaries: Keep this a thin facade — real task bodies live in
    ``api.tasks.servicebus.tasks``.
Key entry points: ``drain_and_resubmit``, ``publish_transitions``, ``dlq_cleanup``.
Risky contracts: Task names are byte-identical to the beat schedule / route
    callers; do not rename without updating ``api.celery_app`` and any enqueue
    sites.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py``.
"""

from __future__ import annotations

from api.tasks.servicebus.tasks import (
    dlq_cleanup,
    drain_and_resubmit,
    publish_transitions,
)

__all__ = ["dlq_cleanup", "drain_and_resubmit", "publish_transitions"]
