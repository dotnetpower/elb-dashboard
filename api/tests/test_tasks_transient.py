"""Tests for the shared beat-task transient-infra-error guard.

Responsibility: Lock the ``skip_tick_on_transient_infra`` contract — transient
    azure-core / socket errors become a ``{"skipped": "transient"}`` result,
    everything else propagates, and the wrapper forwards ``*args``/``**kwargs``
    (so ``bind=True`` tasks keep working).
Edit boundaries: Pure unit tests; no Celery app, no Azure calls.
Key entry points: ``test_*``.
Risky contracts: Mirrors the production decorator stacking order (the function
    under test is called directly, not through Celery).
Validation: ``uv run pytest -q api/tests/test_tasks_transient.py``.
"""

from __future__ import annotations

import pytest
from api.tasks.transient import is_transient_infra_error, skip_tick_on_transient_infra
from azure.core.exceptions import ServiceRequestError, ServiceResponseError


def test_classifier_matches_transient_errors() -> None:
    assert is_transient_infra_error(ServiceRequestError("dns"))
    assert is_transient_infra_error(ServiceResponseError("reset"))
    assert is_transient_infra_error(ConnectionError("socket"))
    assert not is_transient_infra_error(ValueError("bug"))


def test_skip_on_service_request_error() -> None:
    @skip_tick_on_transient_infra
    def task() -> dict[str, object]:
        raise ServiceRequestError(
            "Failed to resolve 'x.table.core.windows.net' "
            "([Errno -3] Temporary failure in name resolution)"
        )

    out = task()
    assert out == {"skipped": "transient", "error_class": "ServiceRequestError"}


def test_non_transient_propagates() -> None:
    @skip_tick_on_transient_infra
    def task() -> dict[str, object]:
        raise ValueError("genuine bug")

    with pytest.raises(ValueError, match="genuine bug"):
        task()


def test_forwards_args_and_kwargs_for_bind_true() -> None:
    # bind=True tasks receive `self` positionally plus keyword arguments; the
    # wrapper must forward both verbatim and return the task's own result on the
    # happy path.
    seen: dict[str, object] = {}

    @skip_tick_on_transient_infra
    def task(self_obj: object, *, limit: int = 0) -> dict[str, object]:
        seen["self"] = self_obj
        seen["limit"] = limit
        return {"ok": True, "limit": limit}

    sentinel = object()
    out = task(sentinel, limit=200)
    assert out == {"ok": True, "limit": 200}
    assert seen == {"self": sentinel, "limit": 200}
