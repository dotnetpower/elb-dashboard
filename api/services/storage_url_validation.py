"""Compatibility wrapper for `api.services.storage.url_validation`.

Responsibility: Re-export `api.services.storage.url_validation` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.storage.url_validation`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from api.services.storage.url_validation import (
    absolute_blob_url,
    validate_storage_blob_reference,
)

__all__ = [
    "absolute_blob_url",
    "validate_storage_blob_reference",
]
