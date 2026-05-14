"""Celery task modules for the elb-dashboard control plane.

Each module is auto-discovered by the Celery worker via the ``include``
list in ``api.celery_app``.
"""

from __future__ import annotations

# Import task modules so Celery auto-discovers @shared_task decorators.
from api.tasks import acr, azure, blast, storage  # noqa: F401

__all__ = ["acr", "azure", "blast", "storage"]
