"""Compatibility wrapper for `api.services.openapi.deployment`.

Responsibility: Re-export `api.services.openapi.deployment` at the legacy flat path.
Edit boundaries: Implementation lives in `api.services.openapi.deployment`; do not add logic here.
Key entry points: Module import side effects and constants.
Risky contracts: Keep `__all__` in sync with the underlying module's public surface.
Validation: `uv run pytest -q api/tests/test_openapi_deployment.py`.
"""

from api.services.openapi.deployment import (
    OpenApiDeploymentError,
    _container_image,
    _deployment_url,
    _image_repository,
    _image_tag,
    _read_deployment,
    get_openapi_deployment_status,
)

__all__ = [
    "OpenApiDeploymentError",
    "_container_image",
    "_deployment_url",
    "_image_repository",
    "_image_tag",
    "_read_deployment",
    "get_openapi_deployment_status",
]
