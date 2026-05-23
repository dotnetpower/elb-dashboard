"""OpenAPI deployment, runtime configuration, and token management services.

Responsibility: Group OpenAPI-related service modules.
Edit boundaries: Submodules own their logic; this package only aggregates exports.
Key entry points: `deployment`, `runtime`, `token` submodules.
Risky contracts: Keep credentials centralized; never log tokens.
Validation: `uv run pytest -q api/tests/test_openapi_deployment.py api/tests/test_openapi_token.py`.
"""

from __future__ import annotations

__all__: list[str] = []
