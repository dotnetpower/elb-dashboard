"""Shared test-double package for `api/tests/`.

Responsibility: Re-export the shared test helpers so tests can `from api.tests._fakes
    import make_async_result, make_send_task_recorder, …` instead of memorising the
    submodule layout.
Edit boundaries: Re-exports only. Real helper code lives in sibling modules
    (`celery.py`, …). Add a new submodule when a third file needs the same fake.
Key entry points: `AsyncResultStub`, `make_async_result`, `make_send_task_recorder`,
    `make_delay_recorder`.
Risky contracts: This package is `_fakes` (underscore-prefixed) so it stays clearly
    test-only and is not picked up by pytest collection as `test_*.py`.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

from api.tests._fakes.celery import (
    AsyncResultStub,
    make_async_result,
    make_delay_recorder,
    make_send_task_recorder,
)

__all__ = (
    "AsyncResultStub",
    "make_async_result",
    "make_delay_recorder",
    "make_send_task_recorder",
)
