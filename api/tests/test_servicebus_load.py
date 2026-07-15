"""Load / stress tests for the Service Bus drain path and the OpenAPI rate limiter.

Responsibility: Prove the queue-heavy and ``/v1/jobs``-heavy paths stay correct
    and bounded under load — a large request-queue backlog drains fully over
    several bounded ticks with one submit per distinct correlation id (no loss,
    no duplicate submit), at-least-once redelivery is deduped, a permanent-
    rejection flood dead-letters without an infinite retry loop, and the OpenAPI
    rate-limiter never over-admits under concurrent callers.
Edit boundaries: Behaviour-under-load only. The Service Bus SDK client/receiver
    and the OpenAPI submit call are faked; bridge tracking uses the local file
    backend so idempotency is exercised for real.
Key entry points: the ``test_*`` functions.
Risky contracts: drain is bounded by ``_DRAIN_MAX_MESSAGES`` per tick; the drain
    handler is idempotent on ``external_correlation_id``; a 4xx submit rejection
    dead-letters (no retry storm); the rate-limiter sliding window is
    thread-safe (exactly ``max_requests`` admitted per window).
Validation: ``uv run pytest -q api/tests/test_servicebus_load.py``.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any

import pytest
from api.services import external_blast, service_bus
from api.services.service_bus_pref import ServiceBusConfig
from api.tasks.servicebus import tasks as sb_tasks
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _file_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the local file backend for bridge tracking + state so the load test
    # never reaches a real Table and idempotency is exercised end-to-end.
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sb_tasks,
        "_execution_admission_for_drain",
        lambda _cfg: {"allowed": True, "reason": "ready"},
    )
    monkeypatch.setattr(sb_tasks, "_acquire_drain_lock", lambda _queue="": (True, "test"))
    monkeypatch.setattr(sb_tasks, "_release_drain_lock", lambda *_args: None)


# --------------------------------------------------------------------------- #
# Fakes — a multi-tick peek-lock receiver.
# --------------------------------------------------------------------------- #


class _LoadMessage:
    def __init__(self, message_id: str, body: dict[str, Any]) -> None:
        self.message_id = message_id
        self.sequence_number = hash(message_id) & 0xFFFFFFFF
        self.correlation_id = body.get("external_correlation_id") or message_id
        self.subject = "blast.request"
        self.content_type = "application/json"
        self.enqueued_time_utc = None
        self.application_properties: dict[str, Any] = {}
        self.dead_letter_reason = None
        self.delivery_count = 0
        self._raw = json.dumps(body).encode("utf-8")

    @property
    def body(self):
        return [self._raw]


class _LoadReceiver:
    """Peek-lock receiver: completed/dead-lettered messages are removed from the
    queue, abandoned messages become receivable again (next tick)."""

    def __init__(self, messages: list[_LoadMessage]) -> None:
        self.available = list(messages)
        self.completed: list[str] = []
        self.dead_lettered: list[str] = []
        self.abandoned: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def receive_messages(self, max_message_count: int, max_wait_time: int = 0):
        batch = self.available[:max_message_count]
        self.available = self.available[max_message_count:]
        return batch

    def complete_message(self, message: _LoadMessage) -> None:
        self.completed.append(message.message_id)

    def dead_letter_message(self, message: _LoadMessage, reason: str = "") -> None:
        self.dead_lettered.append(message.message_id)

    def abandon_message(self, message: _LoadMessage) -> None:
        self.abandoned.append(message.message_id)
        # Real-broker behaviour: an abandoned message is receivable again.
        self.available.append(message)


class _LoadClient:
    def __init__(self, receiver: _LoadReceiver) -> None:
        self._receiver = receiver

    def get_queue_receiver(self, *_a: Any, **_k: Any) -> _LoadReceiver:
        return self._receiver


def _patch_client(monkeypatch: pytest.MonkeyPatch, receiver: _LoadReceiver) -> None:
    @contextmanager
    def fake_client(_cfg_arg: ServiceBusConfig):
        yield _LoadClient(receiver)

    monkeypatch.setattr(service_bus, "_client", fake_client)


def _enable(monkeypatch: pytest.MonkeyPatch) -> ServiceBusConfig:
    cfg = ServiceBusConfig(
        enabled=True,
        auth_mode="entra",
        namespace_fqdn="sb-elb-dashboard-krc.servicebus.windows.net",
    )
    monkeypatch.setattr(sb_tasks, "service_bus_enabled", lambda: True)
    monkeypatch.setattr(sb_tasks, "get_service_bus_config", lambda: cfg)
    # Keep the heavy best-effort persistence out of the hot loop; it is covered
    # by its own tests. The drain contract under test is settle + dedup + bound.
    monkeypatch.setattr(sb_tasks, "_persist_drain_row_and_trace", lambda *a, **k: None)
    monkeypatch.setattr(service_bus, "publish_event", lambda *a, **k: None)
    return cfg


def _request_body(corr: str) -> dict[str, Any]:
    return {
        "program": "blastn",
        "db": "core_nt",
        "query_fasta": ">s\nACGTACGTACGT",
        "external_correlation_id": corr,
    }


def _drain_until_empty(receiver: _LoadReceiver, *, max_ticks: int = 40) -> list[int]:
    """Run drain_and_resubmit ticks until the queue is drained. Returns per-tick
    received counts so the caller can assert the per-tick bound."""
    per_tick: list[int] = []
    ticks = 0
    while receiver.available and ticks < max_ticks:
        out = sb_tasks.drain_and_resubmit()
        per_tick.append(int(out["received"]))
        ticks += 1
    return per_tick


# --------------------------------------------------------------------------- #
# A. Large unique backlog drains fully, bounded, exactly one submit each.
# --------------------------------------------------------------------------- #


def test_large_backlog_drains_fully_bounded_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    n = 300
    messages = [_LoadMessage(f"m{i}", _request_body(f"corr-{i}")) for i in range(n)]
    receiver = _LoadReceiver(messages)
    _patch_client(monkeypatch, receiver)

    submits: list[str] = []
    lock = threading.Lock()

    def fake_submit(payload: dict[str, Any], **_k: Any) -> dict[str, str]:
        with lock:
            submits.append(payload["external_correlation_id"])
        return {"job_id": "op-" + payload["external_correlation_id"]}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)

    per_tick = _drain_until_empty(receiver)

    # No message left behind.
    assert receiver.available == []
    assert len(receiver.completed) == n
    assert receiver.dead_lettered == []
    # Every tick respected the per-tick bound.
    assert per_tick, "expected at least one drain tick"
    assert max(per_tick) <= sb_tasks._DRAIN_MAX_MESSAGES
    # A 300-message backlog cannot drain in one tick at the default bound.
    assert len(per_tick) >= n // sb_tasks._DRAIN_MAX_MESSAGES
    # Exactly one submit per distinct correlation — no loss, no duplicate.
    assert len(submits) == n
    assert len(set(submits)) == n


# --------------------------------------------------------------------------- #
# B. At-least-once redelivery is deduped — duplicates submit once.
# --------------------------------------------------------------------------- #


def test_duplicate_redelivery_is_deduped_under_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    unique = 60
    # Each correlation appears twice (a different message_id, same correlation),
    # interleaved so the duplicate often lands in a later tick than the first.
    messages: list[_LoadMessage] = []
    for i in range(unique):
        messages.append(_LoadMessage(f"a{i}", _request_body(f"corr-{i}")))
    for i in range(unique):
        messages.append(_LoadMessage(f"b{i}", _request_body(f"corr-{i}")))
    receiver = _LoadReceiver(messages)
    _patch_client(monkeypatch, receiver)

    submits: list[str] = []
    monkeypatch.setattr(
        external_blast,
        "submit_job",
        lambda p, **k: submits.append(p["external_correlation_id"])
        or {"job_id": "op-" + p["external_correlation_id"]},
    )

    _drain_until_empty(receiver)

    # All 120 messages settled (completed), but only the 60 distinct
    # correlations were submitted to the sibling.
    assert len(receiver.completed) == unique * 2
    assert len(submits) == unique
    assert len(set(submits)) == unique


# --------------------------------------------------------------------------- #
# C. Permanent-rejection flood dead-letters; no infinite retry storm.
# --------------------------------------------------------------------------- #


def test_permanent_rejection_flood_dead_letters_without_retry_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    n = 120
    messages = [_LoadMessage(f"m{i}", _request_body(f"bad-{i}")) for i in range(n)]
    receiver = _LoadReceiver(messages)
    _patch_client(monkeypatch, receiver)

    calls: list[str] = []

    def reject(payload: dict[str, Any], **_k: Any) -> dict[str, str]:
        calls.append(payload["external_correlation_id"])
        raise HTTPException(status_code=400, detail={"code": "bad_request"})

    monkeypatch.setattr(external_blast, "submit_job", reject)

    per_tick = _drain_until_empty(receiver)

    # Every message is dead-lettered (4xx = permanent), none abandoned/looped.
    assert len(receiver.dead_lettered) == n
    assert receiver.available == []
    assert receiver.abandoned == []
    # The sibling was hit once per message — not ~10x (no delivery-count burn).
    assert len(calls) == n
    # Bounded drain: ceil(n / max) ticks, never an unbounded loop.
    assert len(per_tick) <= (n // sb_tasks._DRAIN_MAX_MESSAGES) + 2


def test_transient_failure_is_abandoned_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx from the sibling must ABANDON (retry), not dead-letter."""
    _enable(monkeypatch)
    receiver = _LoadReceiver([_LoadMessage("m1", _request_body("corr-5xx"))])
    _patch_client(monkeypatch, receiver)

    def overloaded(payload: dict[str, Any], **_k: Any) -> dict[str, str]:
        raise HTTPException(status_code=503, detail={"code": "openapi_unreachable"})

    monkeypatch.setattr(external_blast, "submit_job", overloaded)

    # One tick: the message is abandoned (transient) — not dead-lettered.
    out = sb_tasks.drain_and_resubmit()
    assert out["dead_lettered"] == 0
    assert receiver.dead_lettered == []
    assert "m1" in receiver.abandoned


# --------------------------------------------------------------------------- #
# D. OpenAPI rate limiter: no over-admit under concurrent callers.
# --------------------------------------------------------------------------- #


def test_rate_limiter_no_over_admit_under_concurrency() -> None:
    from api.app.openapi_rate_limit import _SlidingWindowCounter

    counter = _SlidingWindowCounter()
    max_requests = 500
    window = 60.0
    key = "token:loadtest"
    threads_n = 16
    attempts_per_thread = 100  # 1600 attempts >> 500 budget
    allowed = 0
    allowed_lock = threading.Lock()

    def hammer() -> None:
        nonlocal allowed
        local = 0
        for _ in range(attempts_per_thread):
            ok, _retry = counter.check_and_record(
                key, max_requests=max_requests, window_seconds=window
            )
            if ok:
                local += 1
        with allowed_lock:
            allowed += local

    threads = [threading.Thread(target=hammer) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The sliding window admits EXACTLY the budget — never more, even with 16
    # threads racing on the same key (the lock makes check+record atomic).
    assert allowed == max_requests


def test_rate_limiter_distinct_keys_are_independent() -> None:
    from api.app.openapi_rate_limit import _SlidingWindowCounter

    counter = _SlidingWindowCounter()
    max_requests = 10
    window = 60.0
    # Each distinct token gets its own budget — a flood on one key never starves
    # another caller (per-key isolation under load).
    for k in range(20):
        key = f"token:caller-{k}"
        admitted = sum(
            counter.check_and_record(
                key, max_requests=max_requests, window_seconds=window
            )[0]
            for _ in range(max_requests + 5)
        )
        assert admitted == max_requests
