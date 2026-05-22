"""Shared test doubles for `api/tests/`.

Responsibility: Hold the truly repeated, trivial test-double boilerplate that would
    otherwise be re-inlined in many test files (Celery `AsyncResult` stubs, broker
    `send_task` / `.delay` recorders).
Edit boundaries: Add a helper here only when (a) the exact shape repeats in 3+ test
    files AND (b) the helper would still be a minimal interface fake (no growing
    god-object). Context-specific Fakes (`FakeAksClient`, `FakeRepo`, …) intentionally
    stay LOCAL to the test that needs them — do not promote them here.
Key entry points: `make_async_result`, `make_send_task_recorder`,
    `make_delay_recorder`.
Risky contracts: The returned recorder dicts use stable key names (`task_name`,
    `kwargs`, `queue`) that tests assert on. Renaming or reordering breaks every caller.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

from typing import Any

__all__ = (
    "AsyncResultStub",
    "make_async_result",
    "make_delay_recorder",
    "make_send_task_recorder",
)


class AsyncResultStub:
    """Minimal stand-in for `celery.result.AsyncResult`.

    Exposes the three attributes our routes actually read (`id`, `status`,
    `result`). Anything else (`ready()`, `info`, …) raises `AttributeError`
    so a test that needs more is forced to declare a richer local fake.
    """

    __slots__ = ("id", "result", "status")

    def __init__(
        self,
        task_id: str = "task-test",
        *,
        status: str = "PENDING",
        result: Any = None,
    ) -> None:
        self.id = task_id
        self.status = status
        self.result = result


def make_async_result(
    task_id: str = "task-test",
    *,
    status: str = "PENDING",
    result: Any = None,
) -> AsyncResultStub:
    """Return a single `AsyncResultStub` — replaces 1-line `class FakeAsyncResult: id = …`."""
    return AsyncResultStub(task_id, status=status, result=result)


def make_send_task_recorder(
    task_id: str = "task-test",
    *,
    status: str = "PENDING",
) -> tuple[list[dict[str, Any]], Any]:
    """Return `(calls, fake_send_task)` matching `celery_app.send_task(...)` signature.

    The recorder appends one dict per invocation with the keys
    ``task_name``, ``kwargs``, ``queue`` and returns an `AsyncResultStub`
    whose `.id` is `task_id`. Use:

    ```
    calls, fake_send_task = make_send_task_recorder("task-warmup-1")
    monkeypatch.setattr("api.celery_app.celery_app.send_task", fake_send_task)
    ```

    Tests assert on the recorded payload (`assert calls[0]["task_name"] == …`).
    """

    calls: list[dict[str, Any]] = []
    stub = AsyncResultStub(task_id, status=status)

    def fake_send_task(
        task_name: str,
        *,
        kwargs: dict[str, Any] | None = None,
        queue: str | None = None,
        **_extra: Any,
    ) -> AsyncResultStub:
        calls.append(
            {
                "task_name": task_name,
                "kwargs": dict(kwargs or {}),
                "queue": queue,
            }
        )
        return stub

    return calls, fake_send_task


def make_delay_recorder(
    task_id: str = "task-test",
    *,
    status: str = "PENDING",
) -> tuple[list[dict[str, Any]], Any]:
    """Return `(calls, fake_delay)` matching `<task>.delay(**kwargs)`.

    Use to monkeypatch the `.delay` attribute on a Celery task object:

    ```
    calls, fake_delay = make_delay_recorder("task-submit-1")
    monkeypatch.setattr("api.tasks.blast.submit.delay", fake_delay)
    ```
    """

    calls: list[dict[str, Any]] = []
    stub = AsyncResultStub(task_id, status=status)

    def fake_delay(**kwargs: Any) -> AsyncResultStub:
        calls.append(dict(kwargs))
        return stub

    return calls, fake_delay
