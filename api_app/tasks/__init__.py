"""Celery task package.

Phase 1 ships an empty placeholder so the worker and beat containers boot
cleanly without ImportError. Phases 2-3 will populate
`api_app/tasks/{azure,blast,storage}.py` with real handlers.
"""

from __future__ import annotations

# Importing this package registers all task modules with Celery's autodiscovery.
__all__: list[str] = []
