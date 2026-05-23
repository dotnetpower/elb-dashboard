"""FastAPI app composition helpers (inspector, middleware, lifespan, jwt).

Responsibility: Hold the helpers that previously lived in `api.main` so the
entry-point module stays under ~200 LOC and each helper has a single concern.
Edit boundaries: Submodules own their behaviour; this package only aggregates.
Key entry points: `inspector`, `jwt_utils`, `middleware`, `lifespan`.
Risky contracts: Public surface (`RequestIdMiddleware`, `_lifespan`,
`_inspector_should_capture`) must stay importable from `api.main` for back-compat.
Validation: `uv run pytest -q api/tests/test_inspector_exclude.py`.
"""

from __future__ import annotations

__all__: list[str] = []
