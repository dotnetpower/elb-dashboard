"""Shared transient-infra-error guard for beat-scheduled Celery tasks.

Responsibility: Provide one place that classifies a transient connectivity/DNS
    error and a decorator that makes a beat-scheduled task skip the current tick
    (returning ``{"skipped": "transient", ...}``) instead of crashing on it.
Edit boundaries: Pure classification + decorator. No Azure SDK calls, no task
    registration, no business logic. Import the decorator into a task module and
    stack it directly under ``@shared_task``.
Key entry points: ``is_transient_infra_error``, ``skip_tick_on_transient_infra``.
Risky contracts: Only the connectivity/DNS error classes in
    ``_TRANSIENT_INFRA_ERRORS`` are swallowed (and turned into a skip result);
    every other exception propagates unchanged so genuine bugs stay visible. The
    decorated task MUST return a ``dict`` so the skip result is shape-compatible.
    Beat re-runs the task on its next tick (~30 s), so a brief platform blip
    self-heals; the guard exists so the blip does not crash the task with an
    exception Celery cannot pickle (``UnpickleableExceptionWrapper``), which
    floods App Insights with confusing, unactionable exception rows.
Validation: ``uv run pytest -q api/tests/test_tasks_transient.py``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from azure.core.exceptions import ServiceRequestError, ServiceResponseError

LOGGER = logging.getLogger(__name__)

# Transient connectivity/DNS errors a later beat tick retries cleanly. Observed
# in production as `ServiceRequestError("Failed to resolve ... Temporary failure
# in name resolution")` against the workload Storage Table/Blob endpoints during
# a brief platform DNS blip. `ServiceRequestError` / `ServiceResponseError` are
# the azure-core transport-layer wrappers; the builtin `ConnectionError`
# (an `OSError` subclass, parent of `api.services.k8s.cluster_breaker
# .ClusterApiUnreachable`) covers the raw socket/DNS case.
_TRANSIENT_INFRA_ERRORS: tuple[type[BaseException], ...] = (
    ServiceRequestError,
    ServiceResponseError,
    ConnectionError,
)


def is_transient_infra_error(exc: BaseException) -> bool:
    """True for transient connectivity/DNS errors a later beat tick can retry."""
    return isinstance(exc, _TRANSIENT_INFRA_ERRORS)


def skip_tick_on_transient_infra[**P](
    fn: Callable[P, dict[str, Any]],
) -> Callable[P, dict[str, Any]]:
    """Make a beat task skip the tick (not crash) on a transient infra error.

    Converts a transient ``ServiceRequestError`` / ``ServiceResponseError`` /
    ``ConnectionError`` raised from the task body into a
    ``{"skipped": "transient", "error_class": <name>}`` result plus a one-line
    warning. The next beat tick (~30 s) retries, so a brief DNS/network blip
    self-heals without an ``UnpickleableExceptionWrapper`` App Insights
    exception row. Non-transient errors propagate unchanged.

    Stack directly under ``@shared_task`` (works with ``bind=True`` and keyword
    arguments because the wrapper forwards ``*args``/``**kwargs`` verbatim).
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if is_transient_infra_error(exc):
                LOGGER.warning(
                    "%s: transient infra error, skipping tick: %s",
                    fn.__name__,
                    exc,
                )
                return {"skipped": "transient", "error_class": type(exc).__name__}
            raise

    return wrapper
