"""Celery task modules for the elb-dashboard control plane.

Responsibility: Celery task modules for the elb-dashboard control plane
Edit boundaries: Keep long-running side effects here; route handlers should enqueue tasks and
persist state.
Key entry points: `__all__`
Risky contracts: Tasks should be idempotent, retry-aware, and write progress/state checkpoints.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

# Ensure our Celery instance is set as default + current BEFORE any
# `@shared_task` decorator runs. Do not remove or reorder.
from api import celery_app as _celery_app  # noqa: F401

# Import task modules so Celery auto-discovers @shared_task decorators.
from api.tasks import acr, azure, blast, blast_artifacts, servicebus, storage

__all__ = ["acr", "azure", "blast", "blast_artifacts", "servicebus", "storage"]
