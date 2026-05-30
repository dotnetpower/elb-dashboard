"""Tests for `_log_ws_close` — terminal WebSocket close-code metrics.

Responsibility: Verify the `terminal_ws_close` audit line is emitted with
the right fields (code, reason, session_id, hashed identity) for each of
the five distinct close paths in `api/routes/terminal/ws.py::ws_terminal`,
per audit P3 #29. Also verify level routing (INFO for code=1000, WARNING
for everything else) so a log filter pivoted on `level=WARNING` still
catches every failure mode.
Edit boundaries: Unit test only — exercises `_log_ws_close` directly via
caplog. The full WebSocket upgrade path is covered by
`test_terminal_ws_origin.py`; this file focuses on the audit contract.
Key entry points: `test_normal_close_logs_info`,
`test_auth_failure_logs_warning_with_4401`,
`test_origin_failure_logs_warning_with_4403`,
`test_upstream_unavailable_logs_warning_with_1011_and_error_class`,
`test_upstream_error_logs_warning_with_1011`,
`test_oid_and_upn_are_hashed`,
`test_extra_kwargs_appear_in_message`,
`test_unknown_severity_defaults_to_warning`.
Risky contracts: `_log_ws_close` must NEVER log a raw `owner_oid` or
`owner_upn` — always pipe through `redact_oid` so the audit stream never
leaks identifiers (charter §11). Tests assert raw values do not appear in
the captured message.
Validation: `uv run pytest -q api/tests/test_terminal_ws_close_metrics.py`.
"""

from __future__ import annotations

import logging
import re

import api.routes.terminal.ws as ws_module
import pytest
from api.services.sanitise import redact_oid

WS_LOGGER_NAME = ws_module.LOGGER.name


@pytest.fixture()
def caplog_at_info(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture INFO and above against the ws.py logger only."""
    caplog.set_level(logging.INFO, logger=WS_LOGGER_NAME)
    caplog.clear()
    return caplog


def _last_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    matches = [r for r in caplog.records if r.name == WS_LOGGER_NAME]
    assert matches, "expected at least one log record from the ws.py logger"
    return matches[-1]


# ---------------------------------------------------------------------------
# Severity routing — code=1000 is INFO, everything else is WARNING.
# ---------------------------------------------------------------------------


def test_normal_close_logs_info(caplog_at_info: pytest.LogCaptureFixture) -> None:
    ws_module._log_ws_close(code=1000, reason="normal close", session_id="abc123")
    record = _last_record(caplog_at_info)
    assert record.levelno == logging.INFO
    assert "terminal_ws_close" in record.getMessage()
    assert "code=1000" in record.getMessage()
    assert "reason='normal close'" in record.getMessage()
    assert "session_id=abc123" in record.getMessage()


def test_auth_failure_logs_warning_with_4401(
    caplog_at_info: pytest.LogCaptureFixture,
) -> None:
    ws_module._log_ws_close(code=4401, reason="invalid ticket", phase="ticket")
    record = _last_record(caplog_at_info)
    assert record.levelno == logging.WARNING
    msg = record.getMessage()
    assert "code=4401" in msg
    assert "reason='invalid ticket'" in msg
    assert "phase='ticket'" in msg


def test_origin_failure_logs_warning_with_4403(
    caplog_at_info: pytest.LogCaptureFixture,
) -> None:
    ws_module._log_ws_close(
        code=4403,
        reason="origin not allowed",
        session_id="sess-xyz",
        phase="origin",
    )
    record = _last_record(caplog_at_info)
    assert record.levelno == logging.WARNING
    msg = record.getMessage()
    assert "code=4403" in msg
    assert "session_id=sess-xyz" in msg
    assert "phase='origin'" in msg


def test_upstream_unavailable_logs_warning_with_1011_and_error_class(
    caplog_at_info: pytest.LogCaptureFixture,
) -> None:
    ws_module._log_ws_close(
        code=1011,
        reason="upstream unavailable",
        session_id="sess-1",
        phase="upstream_connect",
        error_class="TimeoutError",
    )
    record = _last_record(caplog_at_info)
    assert record.levelno == logging.WARNING
    msg = record.getMessage()
    assert "code=1011" in msg
    assert "reason='upstream unavailable'" in msg
    assert "error_class='TimeoutError'" in msg


def test_upstream_error_logs_warning_with_1011(
    caplog_at_info: pytest.LogCaptureFixture,
) -> None:
    ws_module._log_ws_close(
        code=1011,
        reason="upstream error",
        session_id="sess-2",
        phase="proxy",
        error_class="ConnectionClosedError",
    )
    record = _last_record(caplog_at_info)
    assert record.levelno == logging.WARNING
    msg = record.getMessage()
    assert "phase='proxy'" in msg
    assert "error_class='ConnectionClosedError'" in msg


# ---------------------------------------------------------------------------
# Identity redaction — owner_oid / owner_upn must NEVER appear in plain text.
# ---------------------------------------------------------------------------


def test_oid_and_upn_are_hashed(caplog_at_info: pytest.LogCaptureFixture) -> None:
    raw_oid = "00000000-1111-2222-3333-444444444444"
    raw_upn = "alice@contoso.com"
    expected_oid_hash = redact_oid(raw_oid)
    expected_upn_hash = redact_oid(raw_upn)
    assert expected_oid_hash is not None and expected_oid_hash != raw_oid
    assert expected_upn_hash is not None and expected_upn_hash != raw_upn

    ws_module._log_ws_close(
        code=1000,
        reason="normal close",
        session_id="sess-redact",
        owner_oid=raw_oid,
        owner_upn=raw_upn,
    )
    record = _last_record(caplog_at_info)
    msg = record.getMessage()
    assert raw_oid not in msg, "raw OID leaked into terminal_ws_close audit"
    assert raw_upn not in msg, "raw UPN leaked into terminal_ws_close audit"
    assert f"owner_hash={expected_oid_hash}" in msg
    assert f"upn_hash={expected_upn_hash}" in msg


def test_missing_identity_renders_as_none(caplog_at_info: pytest.LogCaptureFixture) -> None:
    ws_module._log_ws_close(code=1000, reason="normal close", session_id="sess-none")
    msg = _last_record(caplog_at_info).getMessage()
    # `redact_oid(None) -> None`, formatted by the logger as the literal string `None`.
    assert "owner_hash=None" in msg
    assert "upn_hash=None" in msg


# ---------------------------------------------------------------------------
# Extra kwargs and severity fall-through.
# ---------------------------------------------------------------------------


def test_extra_kwargs_appear_in_message(caplog_at_info: pytest.LogCaptureFixture) -> None:
    ws_module._log_ws_close(
        code=1000,
        reason="normal close",
        session_id="sess-extra",
        browser_initiated=True,
        upstream_initiated=False,
    )
    msg = _last_record(caplog_at_info).getMessage()
    assert "browser_initiated=True" in msg
    assert "upstream_initiated=False" in msg


def test_unknown_severity_defaults_to_warning(
    caplog_at_info: pytest.LogCaptureFixture,
) -> None:
    """Any non-1000 code (including custom 4xxx app codes) routes to WARNING.

    Guards against a future regression that adds a special-case (e.g.
    "4999 is INFO") and accidentally drops a real failure off the default
    log filter.
    """
    for code in (1006, 1011, 4401, 4403, 4500, 4999):
        assert ws_module._ws_close_severity(code) == logging.WARNING


def test_terminal_ws_close_message_shape_is_stable(
    caplog_at_info: pytest.LogCaptureFixture,
) -> None:
    """Regression: the leading message tokens must stay stable so any
    downstream log-shipper / KQL parser can pivot on them.

    Format: `terminal_ws_close code=<int> reason=<str> session_id=<str> ...`
    """
    ws_module._log_ws_close(code=1000, reason="normal close", session_id="stable-id")
    msg = _last_record(caplog_at_info).getMessage()
    pattern = re.compile(
        r"^terminal_ws_close code=\d+ reason='[^']*' session_id=\S+ "
        r"owner_hash=\S+ upn_hash=\S+"
    )
    assert pattern.search(msg), (
        f"terminal_ws_close message shape changed; downstream KQL queries "
        f"will break. Got: {msg!r}"
    )
