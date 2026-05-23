"""Compatibility wrapper for `api.services.k8s.monitoring`.

Responsibility: Re-export `api.services.k8s.monitoring` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.k8s.monitoring`; do not add logic here.
Key entry points: Module-level `__getattr__` forwards everything for back-compat.
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
Validation: `uv run pytest -q api/tests/test_k8s_list_events.py api/tests/test_k8s_blast_status.py`.
"""

from typing import Any

from api.services.k8s import monitoring as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
