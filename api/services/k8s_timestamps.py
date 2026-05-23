"""Compatibility wrapper for `api.services.k8s.timestamps`.

Responsibility: Re-export `api.services.k8s.timestamps` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.k8s.timestamps`; do not add logic here.
Key entry points: Module-level `__getattr__` forwards everything for back-compat.
Risky contracts: Sanitise k8s timestamps before HTTP / log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from typing import Any

from api.services.k8s import timestamps as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
