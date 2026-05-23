"""Compatibility wrapper for `api.services.k8s.metrics`.

Responsibility: Re-export `api.services.k8s.metrics` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.k8s.metrics`; do not add logic here.
Key entry points: Module-level `__getattr__` forwards everything for back-compat.
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests`.
"""

from typing import Any

from api.services.k8s import metrics as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
