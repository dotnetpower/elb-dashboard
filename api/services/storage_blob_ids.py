"""Compatibility wrapper for `api.services.storage.blob_ids`.

Responsibility: Re-export the storage blob identifier helper module at the
legacy flat service path.
Edit boundaries: Real implementation lives in `api.services.storage.blob_ids`;
do not add logic here.
Key entry points: Module-level `__getattr__` and `__dir__`.
Risky contracts: Keep this shim registered in `test_services_facade_contract.py`
so package splits do not silently break legacy imports.
Validation: `uv run pytest -q api/tests/test_services_facade_contract.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.storage import blob_ids as _impl


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return dir(_impl)
