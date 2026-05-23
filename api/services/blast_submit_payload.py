"""Compatibility wrapper for `api.services.blast.submit_payload`.

Responsibility: Re-export `api.services.blast.submit_payload` at the legacy flat path.
Edit boundaries: Real impl lives in `api.services.blast.submit_payload`; do not add logic here.
Key entry points: Module-level `__getattr__` forwards everything for back-compat.
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from typing import Any

from api.services.blast import submit_payload as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
