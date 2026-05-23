"""Compatibility wrapper for `api.services.openapi.runtime`.

Responsibility: Re-export `api.services.openapi.runtime` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.openapi.runtime`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_openapi_proxy_route.py`.
"""

from api.services.openapi.runtime import (
    _normalise_base_url,
    _redis_url,
    get_openapi_api_token,
    get_openapi_base_url,
    save_openapi_api_token,
    save_openapi_base_url,
)

__all__ = [
    "_normalise_base_url",
    "_redis_url",
    "get_openapi_api_token",
    "get_openapi_base_url",
    "save_openapi_api_token",
    "save_openapi_base_url",
]
