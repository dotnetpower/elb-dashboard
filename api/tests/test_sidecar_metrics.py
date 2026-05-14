"""Tests for sidecar_metrics aggregation logic.

Live Redis interaction (`MGET`, `INFO`) is covered by the docker-compose
smoke step in the change note; here we verify the staleness classifier
which is the only piece of branchy business logic.
"""

from __future__ import annotations

from api.services.sidecar_metrics import _classify


def test_classify_missing_payload_is_down() -> None:
    assert _classify(now=100.0, payload=None) == "down"


def test_classify_fresh_payload_is_ok() -> None:
    assert _classify(now=100.0, payload={"ts": 99.5}) == "ok"


def test_classify_aged_payload_is_degraded() -> None:
    # 12s old — past degraded threshold (10s) but inside stale (15s).
    assert _classify(now=100.0, payload={"ts": 88.0}) == "degraded"


def test_classify_stale_payload_is_down() -> None:
    # 30s old — past stale threshold.
    assert _classify(now=100.0, payload={"ts": 70.0}) == "down"


def test_classify_handles_zero_ts() -> None:
    # Defensive: a payload with no ts at all should be treated as down.
    assert _classify(now=100.0, payload={}) == "down"
