"""Tests for sidecar_metrics aggregation logic.

Responsibility: Tests for sidecar_metrics aggregation logic
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `FakeRedis`, `test_classify_missing_payload_is_down`,
`test_classify_fresh_payload_is_ok`, `test_classify_aged_payload_is_degraded`,
`test_classify_stale_payload_is_down`, `test_classify_handles_zero_ts`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_sidecar_metrics.py`.
"""

from __future__ import annotations

import json
import time
from typing import Any

import redis
from api.services.sidecar_metrics import (
    ALL_SIDECARS,
    RedisCpuSampler,
    _apply_local_probe_fallbacks,
    _classify,
    collect_snapshot,
)


class FakeRedis:
    def __init__(
        self,
        raw_entries: list[object] | None = None,
        *,
        fail_mget: bool = False,
        fail_info: bool = False,
    ) -> None:
        self.raw_entries = raw_entries or []
        self.fail_mget = fail_mget
        self.fail_info = fail_info

    def mget(self, _keys: list[str]) -> list[object]:
        if self.fail_mget:
            raise redis.RedisError("redis is unavailable")
        return self.raw_entries

    def info(self, section: str) -> dict[str, Any]:
        if self.fail_info:
            raise redis.RedisError("info failed")
        if section == "memory":
            return {"used_memory": 1024, "maxmemory": 0}
        if section == "cpu":
            return {"used_cpu_sys": 1.0, "used_cpu_user": 2.0}
        if section == "server":
            return {"redis_version": "7.2-test"}
        return {}


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


def test_classify_handles_bad_ts() -> None:
    assert _classify(now=100.0, payload={"ts": "not-a-number"}) == "down"


def test_collect_snapshot_isolates_bad_reporter_payloads(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_SIDECAR_PROBES", "false")
    now = time.time()
    client = FakeRedis(
        [
            json.dumps({"name": "wrong", "ts": now, "cpu_pct": 1.2}),
            b"{bad-json",
            json.dumps(["not", "a", "dict"]),
            json.dumps({"ts": "not-a-number"}),
            None,
        ]
    )

    snapshot = collect_snapshot(client=client)  # type: ignore[arg-type]
    sidecars = snapshot["sidecars"]

    assert sidecars["frontend"]["name"] == "frontend"
    assert sidecars["frontend"]["health"] == "ok"
    assert sidecars["api"]["_error"] == "bad_json"
    assert sidecars["worker"]["_error"] == "bad_payload"
    assert sidecars["beat"]["_error"] == "bad_ts"
    assert sidecars["terminal"]["_error"] == "missing"
    assert sidecars["redis"]["health"] == "ok"


def test_collect_snapshot_returns_all_down_when_redis_unavailable() -> None:
    snapshot = collect_snapshot(client=FakeRedis(fail_mget=True))  # type: ignore[arg-type]

    assert snapshot["degraded"] is True
    assert snapshot["degraded_reason"] == "redis_unavailable"
    assert set(snapshot["sidecars"]) == set(ALL_SIDECARS)
    assert {entry["health"] for entry in snapshot["sidecars"].values()} == {"down"}


def test_collect_snapshot_degrades_only_redis_when_info_fails() -> None:
    now = time.time()
    client = FakeRedis(
        [json.dumps({"name": name, "ts": now}) for name in ALL_SIDECARS if name != "redis"],
        fail_info=True,
    )

    snapshot = collect_snapshot(client=client)  # type: ignore[arg-type]

    assert snapshot["sidecars"]["frontend"]["health"] == "ok"
    assert snapshot["sidecars"]["redis"]["health"] == "degraded"
    assert snapshot["sidecars"]["redis"]["_error"] == "redis_info_failed"


def test_local_probe_fallback_marks_missing_frontend_and_terminal_ok(monkeypatch) -> None:
    monkeypatch.setenv("CONTAINER_APP_REVISION", "local")
    now = time.time()
    sidecars = {
        "frontend": {"name": "frontend", "health": "down", "ts": None, "_error": "missing"},
        "terminal": {"name": "terminal", "health": "down", "ts": None, "_error": "missing"},
        "api": {"name": "api", "health": "ok", "ts": now},
    }

    _apply_local_probe_fallbacks(sidecars, now, probe_http_ok=lambda _url: True)

    assert sidecars["frontend"]["health"] == "ok"
    assert sidecars["frontend"]["source"] == "local_probe"
    assert sidecars["terminal"]["health"] == "ok"
    assert sidecars["terminal"]["source"] == "local_probe"


def test_local_probe_fallback_does_not_override_reported_down(monkeypatch) -> None:
    monkeypatch.setenv("CONTAINER_APP_REVISION", "local")
    sidecars = {
        "frontend": {"name": "frontend", "health": "down", "ts": None, "_error": "bad_ts"},
    }

    _apply_local_probe_fallbacks(sidecars, time.time(), probe_http_ok=lambda _url: True)

    assert sidecars["frontend"]["health"] == "down"
    assert sidecars["frontend"]["_error"] == "bad_ts"


def test_local_probe_fallback_is_disabled_for_deployed_revisions(monkeypatch) -> None:
    monkeypatch.setenv("CONTAINER_APP_REVISION", "ca-elb-dashboard--0000001")
    sidecars = {
        "frontend": {"name": "frontend", "health": "down", "ts": None, "_error": "missing"},
    }

    _apply_local_probe_fallbacks(sidecars, time.time(), probe_http_ok=lambda _url: True)

    assert sidecars["frontend"]["health"] == "down"


def test_redis_cpu_sampler_uses_deltas() -> None:
    sampler = RedisCpuSampler()

    assert sampler.percent(now=10.0, cpu_total=4.0) == 0.0
    assert sampler.percent(now=12.0, cpu_total=5.0) == 50.0
