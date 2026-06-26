"""Tests for the #78 footgun guard on the demo external SB consumer.

Responsibility: Lock that starting the demo consumer with the shared 'default'
subscription emits the conflict WARNING (so a 50/50 abandon storm with a real
integrator is visible at startup, not weeks later in DLQ growth).
Edit boundaries: Test-only; monkeypatches the gate + thread start so no Service
Bus connection is opened.
Key entry points: pytest test functions.
Risky contracts: the WARNING must fire whenever the resolved subscriptions list
contains a case-insensitive 'default'.
Validation: ``uv run pytest -q api/tests/test_external_consumer_footgun.py``.
"""

from __future__ import annotations

import logging

import pytest
from api.services import service_bus_external_consumer as ext


def _start_with_subs(
    monkeypatch: pytest.MonkeyPatch, subs: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ext, "external_consumer_enabled", lambda: True)
    monkeypatch.setattr(ext, "completion_subscriptions", lambda: subs)

    class FakeThread:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(ext.threading, "Thread", FakeThread)
    ext.reset_external_consumer_state_for_test()
    with caplog.at_level(logging.WARNING, logger=ext.LOGGER.name):
        ext.start_external_consumer()
    ext.reset_external_consumer_state_for_test()


def test_warns_when_draining_shared_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _start_with_subs(monkeypatch, ["dash-demo", "default"], caplog)
    assert any("competing" in r.message or "compete" in r.message for r in caplog.records)


def test_no_warning_when_no_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _start_with_subs(monkeypatch, ["dash-demo"], caplog)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, [r.message for r in warnings]
