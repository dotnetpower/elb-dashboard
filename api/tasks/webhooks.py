"""Celery beat sweep that POSTs terminal-job notifications to a webhook.

Responsibility: Periodically scan recent terminal BLAST jobs and POST a
Slack/Teams/Discord-compatible message to the configured webhook for any job not
yet notified, marking it sent. Gated by ``WEBHOOK_NOTIFICATIONS_ENABLED`` (default
OFF) AND the stored config's ``enabled`` flag.
Edit boundaries: This module owns the send + the per-job ``_webhook_sent`` marker.
Config + the SSRF URL guard live in ``api/services/webhooks_pref.py``; job listing
in ``JobStateRepository.list_recent_terminal``.
Key entry points: ``dispatch_job_webhooks`` (beat task), ``build_message``,
``post_webhook``.
Risky contracts: the URL is re-validated through ``validate_webhook_url`` at send
time (SSRF guard, in case the allowlist tightened after the config was saved).
Delivery is at-least-once: a POST that succeeds but whose marker write fails may
re-send next sweep — acceptable for a notification. Bounded per sweep + time
window.
Validation: ``uv run pytest -q api/tests/test_webhooks_task.py``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

LOGGER = logging.getLogger(__name__)

_EMOJI = {"completed": "\u2705", "failed": "\u274c", "cancelled": "\u23f8\ufe0f"}
_POST_TIMEOUT_SECONDS = 5.0


def _gate_enabled() -> bool:
    return os.environ.get("WEBHOOK_NOTIFICATIONS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _sweep_cap() -> int:
    raw = os.environ.get("WEBHOOK_SWEEP_LIMIT", "").strip()
    if not raw:
        return 20
    try:
        value = int(raw)
    except ValueError:
        return 20
    return max(1, min(value, 100))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_message(state: Any) -> dict[str, Any]:
    """Build a Slack/Teams/Discord-compatible JSON body for a terminal job.

    ``text`` is read by Slack + Teams (legacy connector); ``content`` by Discord.
    Sending both keeps a single payload working across the common providers.
    """
    status = str(getattr(state, "status", "") or "")
    emoji = _EMOJI.get(status, "\u26a0\ufe0f")
    title = str(getattr(state, "job_title", "") or getattr(state, "job_id", "") or "BLAST job")
    program = str(getattr(state, "program", "") or "")
    db = str(getattr(state, "db", "") or "")
    lines = [f"{emoji} BLAST job *{title}* {status}"]
    meta = " \u00b7 ".join(p for p in (program, db) if p)
    if meta:
        lines.append(meta)
    if status == "failed":
        error_code = str(getattr(state, "error_code", "") or "")
        if error_code:
            lines.append(f"error: {error_code}")
    text = "\n".join(lines)
    return {"text": text, "content": text}


def post_webhook(url: str, message: dict[str, Any]) -> bool:
    """POST the message; re-validate the URL (SSRF) first. True on 2xx."""
    from api.services.webhooks_pref import WebhookValidationError, validate_webhook_url

    try:
        validate_webhook_url(url)
    except WebhookValidationError:
        LOGGER.warning("webhook url no longer allowlisted; skipping send")
        return False
    try:
        import httpx

        with httpx.Client(timeout=_POST_TIMEOUT_SECONDS) as client:
            resp = client.post(url, json=message)
            resp.raise_for_status()
        return True
    except Exception as exc:
        LOGGER.info("webhook post failed: %s", type(exc).__name__)
        return False


def _mark_sent(repo: Any, state: Any) -> None:
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    merged = dict(payload)
    merged["_webhook_sent"] = _now_iso()
    repo.update(state.job_id, payload=merged)


@shared_task(name="api.tasks.webhooks.dispatch_job_webhooks", bind=True)
def dispatch_job_webhooks(self: Any, *, scan_limit: int = 200) -> dict[str, Any]:
    """POST terminal-job notifications to the configured webhook (no-op if off)."""
    del self
    summary: dict[str, Any] = {
        "enabled": _gate_enabled(),
        "scanned": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
    }
    if not summary["enabled"]:
        return summary

    try:
        from api.services.webhooks_pref import get_config

        config = get_config()
    except Exception as exc:
        LOGGER.warning("webhook config read failed in sweep: %s", type(exc).__name__)
        return summary
    if config is None or not config.enabled or not config.url:
        return summary

    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = repo.list_recent_terminal(limit=scan_limit)
    except Exception as exc:
        LOGGER.warning("webhook sweep listing failed: %s", type(exc).__name__)
        return summary

    summary["scanned"] = len(rows)
    cap = _sweep_cap()
    for state in rows:
        if summary["sent"] >= cap:
            break
        payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
        if payload.get("_webhook_sent"):
            summary["skipped"] += 1
            continue
        status = str(getattr(state, "status", "") or "")
        if config.events == "failed_only" and status != "failed":
            summary["skipped"] += 1
            continue
        if post_webhook(config.url, build_message(state)):
            try:
                _mark_sent(repo, state)
            except Exception as exc:
                LOGGER.info(
                    "webhook marker write failed job_id=%s: %s",
                    getattr(state, "job_id", "?"),
                    type(exc).__name__,
                )
            summary["sent"] += 1
        else:
            summary["failed"] += 1

    if summary["sent"] or summary["failed"]:
        LOGGER.info(
            "webhook sweep: scanned=%d sent=%d failed=%d skipped=%d",
            summary["scanned"],
            summary["sent"],
            summary["failed"],
            summary["skipped"],
        )
    return summary
