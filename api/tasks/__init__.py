"""Celery task package.

Phase 1 ships an empty placeholder so the worker and beat containers boot
cleanly without ImportError. Phases 2-3 will populate
`api/tasks/{azure,blast,storage}.py` with real handlers.
"""

from __future__ import annotations

# Importing this package registers all task modules with Celery's autodiscovery.
"""Celery task modules for the elb-dashboard control plane.

Each module is auto-discovered by the Celery worker via the ``include``
list in ``api.celery_app``.
"""

from __future__ import annotations

# Import task modules so Celery auto-discovers @shared_task decorators.
from api.tasks import acr, azure, blast, storage  # noqa: F401

__all__ = ["acr", "azure", "blast", "storage"]
