"""Tests for ARM discovery-read failure logging.

Responsibility: Verify that the expected out-of-scope ``AuthorizationFailed``
on a discovery read (issue #46) is logged as a one-line warning without a stack
trace, while genuine faults keep their full ``exc_info`` trace.
Edit boundaries: Logging classification only — do not exercise real ARM.
Key entry points: `test_authorization_failure_logs_without_stack`,
`test_generic_failure_keeps_stack`,
`test_is_expected_authorization_failure_classification`.
Risky contracts: Keep the AuthorizationFailed downgrade scoped so real faults
still surface as App Insights exception rows.
Validation: `uv run pytest -q api/tests/test_arm_discovery_logging.py`.
"""

from __future__ import annotations

import logging

from api.routes import arm
from azure.core.exceptions import HttpResponseError


def _authz_error() -> HttpResponseError:
    exc = HttpResponseError(message="AuthorizationFailed: not authorized")
    exc.status_code = 403
    return exc


def test_is_expected_authorization_failure_classification() -> None:
    assert arm._is_expected_authorization_failure(_authz_error()) is True

    code_only = HttpResponseError(message="denied")
    code_only.error = type("E", (), {"code": "AuthorizationFailed"})()
    assert arm._is_expected_authorization_failure(code_only) is True

    other = HttpResponseError(message="boom")
    other.status_code = 500
    assert arm._is_expected_authorization_failure(other) is False
    assert arm._is_expected_authorization_failure(RuntimeError("nope")) is False


def test_authorization_failure_logs_without_stack(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING, logger=arm.LOGGER.name):
        arm._log_discovery_failure("list_resource_groups", _authz_error())
    records = [r for r in caplog.records if "list_resource_groups" in r.getMessage()]
    assert records, "expected a warning record"
    # The handled/expected case must NOT carry a stack trace (no exception row).
    assert all(r.exc_info is None for r in records)
    assert any("AuthorizationFailed" in r.getMessage() for r in records)


def test_generic_failure_keeps_stack(caplog) -> None:  # type: ignore[no-untyped-def]
    boom = HttpResponseError(message="internal")
    boom.status_code = 500
    with caplog.at_level(logging.WARNING, logger=arm.LOGGER.name):
        arm._log_discovery_failure("list_resource_groups", boom)
    records = [r for r in caplog.records if "list_resource_groups" in r.getMessage()]
    assert records
    # A genuine fault keeps its full stack so it still surfaces in App Insights.
    assert any(r.exc_info is not None for r in records)
