"""Behaviour tests for the shared log-dedup helper.

Responsibility: Cover the TTL window, multi-key isolation, exc_info
propagation, and reset hooks of ``dedup_log_warning``.
Edit boundaries: Touches only ``api.services.log_dedup``.
Key entry points: ``dedup_log_warning``, ``reset_dedup_state``.
Risky contracts: The first emission per window MUST log at WARNING (so a
NEW outage class still surfaces in App Insights); repeats MUST drop to
DEBUG (no fresh exception row per polling tick).
Validation: ``uv run pytest -q api/tests/test_log_dedup.py``.
"""

from __future__ import annotations

import logging

import pytest
from api.services import log_dedup


@pytest.fixture(autouse=True)
def _reset() -> None:
    log_dedup.reset_dedup_state()


def test_first_call_logs_warning_repeat_drops_to_debug(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("api.tests.log_dedup.first")
    logger.setLevel(logging.DEBUG)
    key = ("monitor", "aks_list", "ConnectionError")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_dedup.dedup_log_warning(logger, key, "boom %s", "x")
        log_dedup.dedup_log_warning(logger, key, "boom %s", "x")
        log_dedup.dedup_log_warning(logger, key, "boom %s", "x")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert "boom x" in warnings[0].getMessage()
    assert len(debugs) == 2
    assert all("deduped" in r.getMessage() for r in debugs)


def test_distinct_keys_are_isolated(caplog: pytest.LogCaptureFixture) -> None:
    """A new (op, code) tuple must still log at WARNING even when a sibling
    failure class is inside its own dedup window — otherwise a different
    AKS cluster's failure would be hidden by the first one."""
    logger = logging.getLogger("api.tests.log_dedup.isolated")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_dedup.dedup_log_warning(logger, ("aks_warmup", "cluster-a", "ReadTimeout"), "m1")
        log_dedup.dedup_log_warning(logger, ("aks_warmup", "cluster-b", "ReadTimeout"), "m2")
        log_dedup.dedup_log_warning(logger, ("aks_warmup", "cluster-a", "DNSError"), "m3")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["m1", "m2", "m3"]


def test_window_expiry_re_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After the dedup window elapses, the same key must produce a fresh
    WARNING so a sustained-but-newly-resumed outage still surfaces."""
    logger = logging.getLogger("api.tests.log_dedup.expiry")
    logger.setLevel(logging.DEBUG)

    fake_time = [1000.0]

    def fake_monotonic() -> float:
        return fake_time[0]

    monkeypatch.setattr(log_dedup.time, "monotonic", fake_monotonic)
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_dedup.dedup_log_warning(
            logger, ("op", "code"), "first", window_seconds=10.0
        )
        fake_time[0] = 1005.0
        log_dedup.dedup_log_warning(
            logger, ("op", "code"), "second", window_seconds=10.0
        )
        fake_time[0] = 1015.5  # past the 10 s window
        log_dedup.dedup_log_warning(
            logger, ("op", "code"), "third", window_seconds=10.0
        )

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["first", "third"]


def test_exc_info_forwarded_on_first_emission_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first emission must carry the stack trace (so App Insights gets
    one exception row per window); repeats drop the stack so the OTel
    exporter does not record duplicates."""
    logger = logging.getLogger("api.tests.log_dedup.exc_info")
    logger.setLevel(logging.DEBUG)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            log_dedup.dedup_log_warning(logger, ("op",), "failed", exc_info=True)
            log_dedup.dedup_log_warning(logger, ("op",), "failed", exc_info=True)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert len(debugs) == 1
    # Dedup'd debug entries intentionally skip exc_info — they exist only
    # as a local-debug breadcrumb, not as an App Insights exception row.
    assert debugs[0].exc_info is None


def test_reset_dedup_state_clears_map(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("api.tests.log_dedup.reset")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_dedup.dedup_log_warning(logger, ("op",), "once")
        log_dedup.dedup_log_warning(logger, ("op",), "twice")
        log_dedup.reset_dedup_state()
        log_dedup.dedup_log_warning(logger, ("op",), "thrice")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["once", "thrice"]


def test_tracked_keys_bounded(caplog: pytest.LogCaptureFixture) -> None:
    """A pathological caller emitting unique keys forever must not grow
    the internal map without bound — the cap evicts oldest entries."""
    logger = logging.getLogger("api.tests.log_dedup.cap")
    logger.setLevel(logging.WARNING)
    cap_plus = log_dedup._MAX_TRACKED_KEYS + 50
    with caplog.at_level(logging.WARNING, logger=logger.name):
        for i in range(cap_plus):
            log_dedup.dedup_log_warning(logger, ("unique", i), "x")
    # The map should never exceed the cap.
    assert len(log_dedup._LAST_EMITTED) <= log_dedup._MAX_TRACKED_KEYS
    assert len(log_dedup._INSERT_ORDER) <= log_dedup._MAX_TRACKED_KEYS
