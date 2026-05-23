"""Compatibility wrapper for `api.services.storage.usage_cache`.

Responsibility: Re-export `api.services.storage.usage_cache` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.storage.usage_cache`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_storage_usage_cache.py`.
"""

from api.services.storage.usage_cache import (
    UsageCacheResult,
    cached_container_usage_summaries,
    reset_storage_usage_cache,
)

__all__ = [
    "UsageCacheResult",
    "cached_container_usage_summaries",
    "reset_storage_usage_cache",
]
