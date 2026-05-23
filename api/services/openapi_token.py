"""Compatibility wrapper for `api.services.openapi.token`.

Responsibility: Re-export `api.services.openapi.token` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.openapi.token`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_openapi_token.py`.
"""

from api.services.openapi.token import (
    OpenApiTokenError,
    _container_env_value,
    _deployment_url,
    _generate_token,
    _mask_token,
    _now_iso,
    _patch_deployment_token,
    _read_deployment,
    _status_payload,
    _sync_runtime_token,
    ensure_openapi_api_token,
    get_openapi_api_token_status,
)

__all__ = [
    "OpenApiTokenError",
    "_container_env_value",
    "_deployment_url",
    "_generate_token",
    "_mask_token",
    "_now_iso",
    "_patch_deployment_token",
    "_read_deployment",
    "_status_payload",
    "_sync_runtime_token",
    "ensure_openapi_api_token",
    "get_openapi_api_token_status",
]
