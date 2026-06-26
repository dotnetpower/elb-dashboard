"""Tests for the webhook dispatch sweep (message build, post guard, sweep).

Responsibility: Cover message shaping, the send-time SSRF re-check, the gate, and
the sweep (sent + marker, failed_only filter, already-sent skip, cap).
Edit boundaries: Test-only; monkeypatches config, repo, and post.
Key entry points: pytest test functions.
Risky contracts: post re-validates the URL; sweep is gated + bounded.
Validation: ``uv run pytest -q api/tests/test_webhooks_task.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from api.tasks import webhooks as wh


@dataclass
class FakeState:
    job_id: str = "job-1"
    status: str = "completed"
    job_title: str = "demo"
    program: str = "blastn"
    db: str = "nt"
    error_code: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class FakeRepo:
    def __init__(self, rows: list[FakeState]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def list_recent_terminal(self, *, limit: int = 200) -> list[FakeState]:
        del limit
        return list(self._rows)

    def update(self, job_id: str, **kwargs: Any) -> None:
        self.updates.append((job_id, kwargs))


def test_build_message_completed() -> None:
    msg = wh.build_message(FakeState(status="completed", job_title="t"))
    assert "completed" in msg["text"]
    assert msg["content"] == msg["text"]


def test_build_message_failed_includes_error() -> None:
    msg = wh.build_message(FakeState(status="failed", error_code="terminal_az_login_failed"))
    assert "terminal_az_login_failed" in msg["text"]


def test_provider_detection() -> None:
    assert wh.detect_provider("https://hooks.slack.com/services/a/b/c") == "slack"
    assert wh.detect_provider("https://my.webhook.office.com/x") == "teams"
    assert wh.detect_provider("https://my.webhook.office365.us/x") == "teams"
    assert wh.detect_provider("https://discord.com/api/webhooks/1/x") == "discord"
    assert wh.detect_provider("https://discordapp.com/api/webhooks/1/x") == "discord"
    assert wh.detect_provider("https://my.logic.azure.com/workflows/x") == "generic"
    assert wh.detect_provider("") == "generic"
    assert wh.detect_provider("not a url") == "generic"


def test_slack_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.control_plane_url.resolve_control_plane_url",
        lambda: ("https://dash.example.com", "env"),
    )
    msg = wh.build_message(
        FakeState(status="completed", job_id="j-1", job_title="demo"),
        url="https://hooks.slack.com/services/a/b/c",
    )
    assert "blocks" in msg
    assert msg["text"]  # fallback text required for mobile/SR
    types = [b["type"] for b in msg["blocks"]]
    assert "header" in types and "section" in types and "actions" in types
    # The Open-in-dashboard button URL is built off the canonical base.
    button = next(b for b in msg["blocks"] if b["type"] == "actions")
    assert button["elements"][0]["url"] == "https://dash.example.com/blast/jobs/j-1"


def test_teams_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.control_plane_url.resolve_control_plane_url",
        lambda: ("https://dash.example.com", "env"),
    )
    msg = wh.build_message(
        FakeState(status="failed", error_code="terminal_az_login_failed", job_id="j-2"),
        url="https://t1.webhook.office.com/webhookb2/x",
    )
    assert msg["@type"] == "MessageCard"
    assert msg["themeColor"] == "E01E5A"
    assert msg["potentialAction"][0]["targets"][0]["uri"].endswith("/blast/jobs/j-2")
    facts = msg["sections"][0]["facts"]
    assert any(f["value"] == "terminal_az_login_failed" for f in facts)


def test_discord_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.control_plane_url.resolve_control_plane_url",
        lambda: ("https://dash.example.com", "env"),
    )
    msg = wh.build_message(
        FakeState(status="cancelled", job_id="j-3"),
        url="https://discord.com/api/webhooks/1/x",
    )
    assert msg["content"]
    assert msg["embeds"][0]["color"] == 0xECB22E
    assert msg["embeds"][0]["url"].endswith("/blast/jobs/j-3")


def test_generic_payload_is_backward_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.services.control_plane_url.resolve_control_plane_url",
        lambda: ("", "none"),
    )
    msg = wh.build_message(
        FakeState(status="completed"),
        url="https://my.logic.azure.com/workflows/x",
    )
    # Legacy {text, content} shape must remain so custom integrations keep working.
    assert set(msg.keys()) >= {"text", "content"}
    assert msg["text"] == msg["content"]
    assert "blocks" not in msg and "embeds" not in msg


def test_no_button_when_base_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable base URL must not crash the builder — just skip the link."""
    monkeypatch.setattr(
        "api.services.control_plane_url.resolve_control_plane_url",
        lambda: ("", "none"),
    )
    msg = wh.build_message(FakeState(), url="https://hooks.slack.com/x/y/z")
    types = [b["type"] for b in msg["blocks"]]
    assert "actions" not in types  # no button when no URL


def test_post_rejects_non_allowlisted_url() -> None:
    assert wh.post_webhook("https://evil.com/x", {"text": "x"}) is False


def test_dispatch_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBHOOK_NOTIFICATIONS_ENABLED", raising=False)
    summary = wh.dispatch_job_webhooks.run()
    assert summary["enabled"] is False
    assert summary["scanned"] == 0


def _cfg(enabled: bool = True, events: str = "terminal") -> Any:
    from api.services.webhooks_pref import WebhookConfig

    return WebhookConfig(
        url="https://hooks.slack.com/services/a/b/c", enabled=enabled, events=events
    )


def _wire(monkeypatch: pytest.MonkeyPatch, repo: FakeRepo, *, post_ok: bool = True) -> list[Any]:
    monkeypatch.setenv("WEBHOOK_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setattr("api.services.webhooks_pref.get_config", lambda: _cfg(), raising=True)
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository", lambda: repo, raising=True
    )
    posts: list[Any] = []
    monkeypatch.setattr(
        wh, "post_webhook", lambda url, msg: (posts.append((url, msg)), post_ok)[1]
    )
    return posts


def test_dispatch_sends_and_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([FakeState()])
    posts = _wire(monkeypatch, repo)
    summary = wh.dispatch_job_webhooks.run()
    assert summary["sent"] == 1
    assert posts
    assert repo.updates and "_webhook_sent" in repo.updates[0][1]["payload"]


def test_dispatch_skips_already_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([FakeState(payload={"_webhook_sent": "2026-06-25T00:00:00+00:00"})])
    _wire(monkeypatch, repo)
    summary = wh.dispatch_job_webhooks.run()
    assert summary["sent"] == 0
    assert summary["skipped"] == 1


def test_dispatch_failed_only_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([FakeState(status="completed")])
    monkeypatch.setenv("WEBHOOK_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setattr(
        "api.services.webhooks_pref.get_config",
        lambda: _cfg(events="failed_only"),
        raising=True,
    )
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository", lambda: repo, raising=True
    )
    monkeypatch.setattr(wh, "post_webhook", lambda url, msg: True)
    summary = wh.dispatch_job_webhooks.run()
    assert summary["sent"] == 0
    assert summary["skipped"] == 1


def test_dispatch_post_failure_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([FakeState()])
    _wire(monkeypatch, repo, post_ok=False)
    summary = wh.dispatch_job_webhooks.run()
    assert summary["failed"] == 1
    assert summary["sent"] == 0
    assert repo.updates == []  # not marked sent on failure
