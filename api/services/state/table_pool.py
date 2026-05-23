"""Pooled TableClient wrapper that keeps the HTTP transport alive across calls.

Responsibility: Hold the `_PooledTableClient` wrapper plus the per-process
"have we ensured this table?" registry. Pure infrastructure — no domain logic.
Edit boundaries: Do not add Azure SDK calls beyond what is needed to keep a
TableClient alive across multiple `with` blocks.
Key entry points: `_PooledTableClient`, `_TABLE_ENDPOINT_ENV`, `_ENSURED_TABLES`,
`_ENSURED_TABLES_LOCK`.
Risky contracts: `__exit__` must NOT close the inner client — that is the whole
point of pooling. The pool is reset only by explicit `.close()` or process exit.
Validation: `uv run pytest -q api/tests/test_state_repo.py`.
"""

from __future__ import annotations

import threading
from typing import Any

_TABLE_ENDPOINT_ENV = "AZURE_TABLE_ENDPOINT"  # eg https://stelb*.table.core.windows.net
_ENSURED_TABLES: set[tuple[str, str]] = set()
_ENSURED_TABLES_LOCK = threading.Lock()


class _PooledTableClient:
    """Reusable wrapper that lets a single TableClient survive multiple ``with`` blocks.

    The Azure Tables SDK closes the underlying HTTP transport on ``__exit__``;
    that would defeat connection pooling for repositories whose methods each
    open a fresh ``with self._state_client() as t:`` block. This wrapper keeps
    the inner client alive across enters so the TLS session and request
    pipeline are reused across calls.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __enter__(self) -> Any:
        return self._inner

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            inner_close()
