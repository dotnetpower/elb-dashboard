"""Tests for the Log Analytics fallback path of the Live Wall log tail.

Responsibility: Verify that `sidecar_logs_la` parses LogsQueryClient responses
  into the existing LogLine schema, that the snapshot cache shares one query
  across containers, and that `sidecar_logs.read_*` dispatches to the LA path
  only when both the Container App env marker and the workspace id are set.
Edit boundaries: No real Azure calls. The `LogsQueryClient` is fully mocked;
  every test isolates global module state via `reset_for_tests`.
Key entry points: see individual `test_*` functions.
Risky contracts: `read_lines_since_la` returns `(lines, max_ts)`; if the
  filter produces no rows the offset must NOT advance (so the SSE loop can
  keep the same watermark for the next tick).
Validation: `uv run pytest -q api/tests/test_sidecar_logs_la.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from api.services import sidecar_logs, sidecar_logs_la


@pytest.fixture(autouse=True)
def _reset_la_module(monkeypatch: pytest.MonkeyPatch) -> None:
    sidecar_logs_la.reset_for_tests()
    monkeypatch.setenv("LOG_ANALYTICS_WORKSPACE_ID", "wsid-test")
    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.delenv("LIVE_WALL_LA_DISABLE", raising=False)
    yield
    sidecar_logs_la.reset_for_tests()


def _fake_response(rows: list[tuple[datetime, str, str]]) -> SimpleNamespace:
    """Build a LogsQueryResult-compatible duck for our parser."""
    table = SimpleNamespace(
        columns=["TimeGenerated", "ContainerName_s", "Log_s"],
        rows=[[ts, name, log] for ts, name, log in rows],
    )
    return SimpleNamespace(tables=[table])


def test_la_fallback_engaged_only_in_container_app(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sidecar_logs._use_la_fallback() is True

    monkeypatch.delenv("CONTAINER_APP_NAME")
    assert sidecar_logs._use_la_fallback() is False

    monkeypatch.setenv("CONTAINER_APP_NAME", "ca-elb-dashboard")
    monkeypatch.delenv("LOG_ANALYTICS_WORKSPACE_ID")
    assert sidecar_logs._use_la_fallback() is False

    monkeypatch.setenv("LOG_ANALYTICS_WORKSPACE_ID", "wsid")
    monkeypatch.setenv("LIVE_WALL_LA_DISABLE", "true")
    assert sidecar_logs._use_la_fallback() is False


def test_read_recent_lines_la_parses_rows_and_sanitizes(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    rows = [
        (now - timedelta(seconds=2), "api", "GET /api/health 200 OK"),
        (
            now - timedelta(seconds=1),
            "api",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        ),
        (now, "worker", "celery task succeeded"),
    ]
    client = MagicMock()
    client.query_workspace.return_value = _fake_response(rows)
    monkeypatch.setattr(sidecar_logs_la, "_get_client", lambda: client)

    api_lines = sidecar_logs_la.read_recent_lines_la("api", tail=10)
    worker_lines = sidecar_logs_la.read_recent_lines_la("worker", tail=10)

    assert next(line["text"] for line in api_lines) == "GET /api/health 200 OK"
    # The masking pipeline runs both the "Bearer …" and the
    # "Authorization: …" patterns, so the rendered line contains two
    # REDACTED tokens. The important assertion is that the original token
    # never appears.
    assert "***REDACTED***" in api_lines[1]["text"]
    assert "abcdefghijklmnopqrstuvwxyz" not in api_lines[1]["text"]
    assert any("celery task succeeded" in line["text"] for line in worker_lines)
    assert "Bearer abcdef" not in " ".join(line["text"] for line in api_lines)
    # Single query covered all containers.
    assert client.query_workspace.call_count == 1


def test_read_lines_since_la_advances_only_on_new_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 5, 27, 1, 0, 0, tzinfo=UTC)
    rows = [
        (base, "api", "first"),
        (base + timedelta(seconds=10), "api", "second"),
        (base + timedelta(seconds=20), "api", "third"),
    ]
    client = MagicMock()
    client.query_workspace.return_value = _fake_response(rows)
    monkeypatch.setattr(sidecar_logs_la, "_get_client", lambda: client)

    second_ms = int((base + timedelta(seconds=10)).timestamp() * 1000)
    lines, new_offset = sidecar_logs_la.read_lines_since_la("api", second_ms)

    assert [line["text"] for line in lines] == ["third"]
    assert new_offset == int((base + timedelta(seconds=20)).timestamp() * 1000)

    # Re-query at the same fresh offset must return no rows AND keep the offset
    # so the SSE loop does not regress.
    again, again_offset = sidecar_logs_la.read_lines_since_la("api", new_offset)
    assert again == []
    assert again_offset == new_offset


def test_snapshot_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.query_workspace.return_value = _fake_response(
        [(datetime.now(UTC), "api", "hello")]
    )
    monkeypatch.setattr(sidecar_logs_la, "_get_client", lambda: client)

    for _ in range(5):
        sidecar_logs_la.read_recent_lines_la("api", tail=5)
        sidecar_logs_la.read_recent_lines_la("worker", tail=5)
        sidecar_logs_la.end_offset_la("frontend")

    assert client.query_workspace.call_count == 1


def test_snapshot_refresh_failure_returns_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 5, 27, 1, 0, 0, tzinfo=UTC)
    client = MagicMock()
    client.query_workspace.return_value = _fake_response([(base, "api", "ok")])
    monkeypatch.setattr(sidecar_logs_la, "_get_client", lambda: client)
    monkeypatch.setattr(sidecar_logs_la, "_CACHE_TTL_SEC", 0.0)  # force every call to refresh

    lines = sidecar_logs_la.read_recent_lines_la("api", tail=5)
    assert [line["text"] for line in lines] == ["ok"]

    client.query_workspace.side_effect = RuntimeError("LA blew up")
    lines = sidecar_logs_la.read_recent_lines_la("api", tail=5)
    # Failure must return the previous snapshot, not an empty list.
    assert [line["text"] for line in lines] == ["ok"]


def test_la_dispatch_from_sidecar_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public `read_recent_lines` must route to LA when fallback is on."""
    client = MagicMock()
    client.query_workspace.return_value = _fake_response(
        [(datetime.now(UTC), "api", "from LA")]
    )
    monkeypatch.setattr(sidecar_logs_la, "_get_client", lambda: client)

    lines = sidecar_logs.read_recent_lines("api", tail=3)
    assert [line["text"] for line in lines] == ["from LA"]


def test_end_offset_la_falls_back_to_now_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.query_workspace.return_value = _fake_response([])
    monkeypatch.setattr(sidecar_logs_la, "_get_client", lambda: client)

    before = sidecar_logs_la._now_ms()
    cursor = sidecar_logs_la.end_offset_la("api")
    after = sidecar_logs_la._now_ms()

    assert before <= cursor <= after
