"""Compatibility wrapper for `api.services.blast.submit_gates`.

Responsibility: Re-export `api.services.blast.submit_gates` at the legacy flat
 path.
Edit boundaries: Real impl lives in `api.services.blast.submit_gates`; do not
add logic here.
Key entry points: Module-level `__getattr__` forwards everything for back-compat.
Risky contracts: Keep gate evaluation centralised in the real module; this shim
is here only because the facade contract test enforces a flat alias for every
``api.services.blast.*`` submodule.
Validation: `uv run pytest -q api/tests/test_services_facade_contract.py`.
"""

from typing import Any

from api.services.blast import submit_gates as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
