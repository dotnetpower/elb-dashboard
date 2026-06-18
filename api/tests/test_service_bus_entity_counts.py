"""Tests for ``service_bus.entity_counts`` telemetry shape.

Responsibility: Verify ``entity_counts`` returns the legacy four counters AND
    the additive ``telemetry`` block (size %, transfer counters, status,
    accessed_at) without breaking when the queue's static properties read
    fails. Also verifies the per-subscription transfer counters are surfaced.
Edit boundaries: Unit-level — admin client and SDK exceptions are stubbed,
    no real Azure call.
Key entry points: the ``test_*`` functions.
Risky contracts: The telemetry block is purely additive; every field
    degrades to ``None`` when the SDK does not expose it.
Validation: ``uv run pytest -q api/tests/test_service_bus_entity_counts.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from api.services import service_bus


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        namespace_fqdn="sb-example.servicebus.windows.net",
        request_queue="elastic-blast-requests",
        completion_topic="elastic-blast-completions",
        auth_mode="entra",
    )


class _FakeAdmin:
    """Stand-in for ``ServiceBusAdministrationClient`` covering the calls
    ``entity_counts`` makes (queue runtime + static + subscriptions)."""

    def __init__(
        self,
        *,
        q_runtime: Any,
        q_props: Any | Exception | None,
        subs: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self._q_runtime = q_runtime
        self._q_props = q_props
        self._subs = subs or []

    def get_queue_runtime_properties(self, _queue: str) -> Any:
        return self._q_runtime

    def get_queue(self, _queue: str) -> Any:
        if isinstance(self._q_props, Exception):
            raise self._q_props
        return self._q_props

    def list_subscriptions(self, _topic: str):
        for sub, _ in self._subs:
            yield sub

    def get_subscription_runtime_properties(self, _topic: str, name: str) -> Any:
        for sub, runtime in self._subs:
            if sub.name == name:
                return runtime
        raise KeyError(name)


@contextmanager
def _patched_admin(monkeypatch: pytest.MonkeyPatch, admin: _FakeAdmin):
    @contextmanager
    def _factory(_cfg_arg: Any):
        yield admin

    monkeypatch.setattr(service_bus, "_admin_client", _factory)
    yield


def test_entity_counts_includes_telemetry_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """The four legacy counters stay, and a ``telemetry`` block carries
    size %, transfer counters, status, and ISO timestamps."""
    q_runtime = SimpleNamespace(
        active_message_count=3,
        dead_letter_message_count=1,
        scheduled_message_count=0,
        total_message_count=4,
        size_in_bytes=512 * 1024,  # 0.5 MiB
        transfer_message_count=0,
        transfer_dead_letter_message_count=2,
        created_at_utc=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        updated_at_utc=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
        accessed_at_utc=datetime(2026, 6, 14, 5, 0, 0, tzinfo=UTC),
    )
    q_props = SimpleNamespace(max_size_in_megabytes=1024, status="Active")
    admin = _FakeAdmin(q_runtime=q_runtime, q_props=q_props)

    with _patched_admin(monkeypatch, admin):
        result = service_bus.entity_counts(_cfg())

    queue = result["queue"]
    # Legacy counters preserved.
    assert queue["active_message_count"] == 3
    assert queue["dead_letter_message_count"] == 1
    assert queue["scheduled_message_count"] == 0
    assert queue["total_message_count"] == 4
    # Additive telemetry surfaced.
    tele = queue["telemetry"]
    assert tele["size_in_bytes"] == 512 * 1024
    assert tele["max_size_in_mb"] == 1024
    # 0.5 MiB out of 1024 MiB = 0.05 %
    assert tele["size_pct"] == pytest.approx(0.05, abs=0.01)
    assert tele["transfer_message_count"] == 0
    assert tele["transfer_dead_letter_message_count"] == 2
    assert tele["status"] == "Active"
    # ISO suffix is ``Z`` (not ``+00:00``) so the SPA can render it directly.
    assert tele["accessed_at"] == "2026-06-14T05:00:00Z"


def test_entity_counts_degrades_when_static_props_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on ``get_queue`` (e.g. SDK transient error) must not blow up
    the counters call — telemetry max/status/size_pct fall back to ``None``."""
    q_runtime = SimpleNamespace(
        active_message_count=0,
        dead_letter_message_count=0,
        scheduled_message_count=0,
        total_message_count=0,
        size_in_bytes=0,
        transfer_message_count=None,
        transfer_dead_letter_message_count=None,
        created_at_utc=None,
        updated_at_utc=None,
        accessed_at_utc=None,
    )
    from azure.servicebus.exceptions import ServiceBusError

    admin = _FakeAdmin(q_runtime=q_runtime, q_props=ServiceBusError("nope"))

    with _patched_admin(monkeypatch, admin):
        result = service_bus.entity_counts(_cfg())

    tele = result["queue"]["telemetry"]
    assert tele["size_in_bytes"] == 0
    assert tele["max_size_in_mb"] is None
    assert tele["size_pct"] is None
    assert tele["status"] is None
    # Legacy counters still present.
    assert result["queue"]["active_message_count"] == 0
    assert result["dead_letter"] == 0


def test_entity_counts_subscription_transfer_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-subscription transfer counters ride along when surfaced by the SDK."""
    q_runtime = SimpleNamespace(
        active_message_count=0,
        dead_letter_message_count=0,
        scheduled_message_count=0,
        total_message_count=0,
        size_in_bytes=0,
        transfer_message_count=None,
        transfer_dead_letter_message_count=None,
        created_at_utc=None,
        updated_at_utc=None,
        accessed_at_utc=None,
    )
    sub = SimpleNamespace(name="dashboard-events")
    sub_runtime = SimpleNamespace(
        active_message_count=2,
        dead_letter_message_count=1,
        transfer_message_count=0,
        transfer_dead_letter_message_count=4,
    )
    admin = _FakeAdmin(
        q_runtime=q_runtime,
        q_props=SimpleNamespace(max_size_in_megabytes=1024, status="Active"),
        subs=[(sub, sub_runtime)],
    )

    with _patched_admin(monkeypatch, admin):
        result = service_bus.entity_counts(_cfg())

    subs = result["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["name"] == "dashboard-events"
    assert subs[0]["active_message_count"] == 2
    assert subs[0]["dead_letter_message_count"] == 1
    assert subs[0]["transfer_message_count"] == 0
    assert subs[0]["transfer_dead_letter_message_count"] == 4


def test_entity_counts_queue_completion_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A queue completion entity surfaces its runtime counts as a single
    pseudo-subscription row (no fan-out / no list_subscriptions call)."""
    q_runtime = SimpleNamespace(
        active_message_count=5,
        dead_letter_message_count=2,
        scheduled_message_count=0,
        total_message_count=7,
        size_in_bytes=0,
        transfer_message_count=1,
        transfer_dead_letter_message_count=0,
        created_at_utc=None,
        updated_at_utc=None,
        accessed_at_utc=None,
    )

    class _NoSubsAdmin(_FakeAdmin):
        def list_subscriptions(self, _topic: str):  # pragma: no cover - must not run
            raise AssertionError("queue completion entity must not list subscriptions")

    admin = _NoSubsAdmin(
        q_runtime=q_runtime,
        q_props=SimpleNamespace(max_size_in_megabytes=1024, status="Active"),
    )
    cfg = SimpleNamespace(
        namespace_fqdn="sb-example.servicebus.windows.net",
        request_queue="elastic-blast-requests",
        completion_topic="elastic-blast-completions",
        completion_kind="queue",
        auth_mode="entra",
    )

    with _patched_admin(monkeypatch, admin):
        result = service_bus.entity_counts(cfg)

    assert result["completion_kind"] == "queue"
    subs = result["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["name"] == "elastic-blast-completions"
    assert subs[0]["active_message_count"] == 5
    assert subs[0]["dead_letter_message_count"] == 2
    assert subs[0]["transfer_message_count"] == 1


def test_pending_request_count_returns_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pending_request_count`` returns the queue's active message count
    (the auto-stop "work in flight" signal)."""
    admin = _FakeAdmin(q_runtime=SimpleNamespace(active_message_count=7), q_props=None)
    with _patched_admin(monkeypatch, admin):
        assert service_bus.pending_request_count(_cfg()) == 7


def test_pending_request_count_none_on_admin_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any admin/runtime failure degrades to ``None`` so an unreadable queue
    never strands a cluster running forever."""

    class _Boom:
        def get_queue_runtime_properties(self, _queue: str) -> Any:
            raise RuntimeError("boom")

    with _patched_admin(monkeypatch, _Boom()):
        assert service_bus.pending_request_count(_cfg()) is None


def test_pending_request_count_none_when_unconfigured() -> None:
    """A config without a namespace yields ``None`` (no admin call)."""
    cfg = SimpleNamespace(
        namespace_fqdn="",
        request_queue="elastic-blast-requests",
        completion_topic="elastic-blast-completions",
        auth_mode="entra",
    )
    assert service_bus.pending_request_count(cfg) is None
