"""Unit tests for the ``api.services.feature_events`` emitter.

Responsibility: Verify ``record_feature_event`` emits a well-formed log record
    on the ``api.events`` logger, carries the customEvent name attribute, drops
    ``None`` attributes, sanitises secrets, escapes reserved LogRecord keys, and
    never raises.
Edit boundaries: Test-only. Keep assertions behavioural (record shape +
    no-raise) — do not assert on Azure Monitor export, which is out of process.
Key entry points: pytest test functions.
Risky contracts: Mirrors the ``microsoft.custom_event.name`` contract the
    deployment relies on for customEvents mapping.
Validation: ``uv run pytest -q api/tests/test_feature_events.py``.
"""

from __future__ import annotations

import logging

import pytest
from api.services.feature_events import _CUSTOM_EVENT_NAME_KEY, record_feature_event


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "api.events"]


def test_emits_record_with_custom_event_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="api.events"):
        record_feature_event("warmup", status="completed", job_id="j1", phase="completed")
    records = _records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, _CUSTOM_EVENT_NAME_KEY) == "warmup"
    assert rec.feature_event == "warmup"
    assert rec.event_status == "completed"
    assert rec.job_id == "j1"
    assert rec.phase == "completed"


def test_drops_none_attributes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="api.events"):
        record_feature_event("warmup", status="failed", job_id="j1", error_code=None)
    rec = _records(caplog)[0]
    assert not hasattr(rec, "error_code")


def test_sanitises_string_attributes(caplog: pytest.LogCaptureFixture) -> None:
    leak = "bearer abcdefghijklmnopqrstuvwxyz0123456789"
    with caplog.at_level(logging.INFO, logger="api.events"):
        record_feature_event("blast_submit", status="failed", error=leak)
    rec = _records(caplog)[0]
    assert "abcdefghijklmnopqrstuvwxyz" not in rec.error


def test_scalar_attributes_pass_through(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="api.events"):
        record_feature_event("warmup", status="completed", shards=10, forced=True)
    rec = _records(caplog)[0]
    assert rec.shards == 10
    assert rec.forced is True


def test_reserved_logrecord_keys_are_escaped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="api.events"):
        # ``module`` and ``name`` collide with built-in LogRecord attributes.
        record_feature_event("cluster_provision", status="completed", module="aks", name="c1")
    rec = _records(caplog)[0]
    assert rec.attr_module == "aks"
    assert rec.attr_name == "c1"
    # The genuine LogRecord.name must remain the logger name.
    assert rec.name == "api.events"


def test_never_raises_on_bad_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("logging exploded")

    import api.services.feature_events as fe

    monkeypatch.setattr(fe.LOGGER, "info", _boom)
    # Must swallow the error rather than propagate to the caller.
    record_feature_event("warmup", status="failed", job_id="j1")
