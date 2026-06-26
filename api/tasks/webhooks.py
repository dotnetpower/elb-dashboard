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
from urllib.parse import urlparse

from celery import shared_task

LOGGER = logging.getLogger(__name__)

_EMOJI = {"completed": "\u2705", "failed": "\u274c", "cancelled": "\u23f8\ufe0f"}
# Single-source-of-truth colour swatch (calm / muted glassmorphic palette) used
# by every provider so Slack / Teams / Discord render the same status colour.
_COLOR_HEX = {"completed": "2EB67D", "failed": "E01E5A", "cancelled": "ECB22E"}
_COLOR_INT = {"completed": 0x2EB67D, "failed": 0xE01E5A, "cancelled": 0xECB22E}
_POST_TIMEOUT_SECONDS = 5.0
# Per-field caps. Slack section text limit is ~3000 chars; Teams accepts more
# but a tight cap keeps a runaway error_code from blowing up the message and
# being silently rejected by the provider.
_MAX_TITLE = 240
_MAX_ERROR = 200


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


def detect_provider(url: str) -> str:
    """Classify a webhook target by host. Returns slack | teams | discord | generic.

    Keyed off the same hosts the SSRF allowlist already accepts
    (``api/services/webhooks_pref.py``). Unknown / Logic Apps / extra-allowlist
    hosts fall through to the generic ``{text, content}`` shape so a custom
    integration is never broken by a provider-specific payload it cannot parse.
    """
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if host == "hooks.slack.com":
        return "slack"
    if host.endswith(".webhook.office.com") or host.endswith(".webhook.office365.us"):
        return "teams"
    if host in ("discord.com", "discordapp.com"):
        return "discord"
    return "generic"


def _resolve_job_url(job_id: str) -> str:
    """Best-effort ``/blast/jobs/{job_id}`` URL on the deployment's public host."""
    try:
        from urllib.parse import quote

        from api.services.control_plane_url import resolve_control_plane_url

        base, _source = resolve_control_plane_url()
        if not base:
            return ""
        return f"{base.rstrip('/')}/blast/jobs/{quote(str(job_id or ''), safe='')}"
    except Exception:
        return ""


def _message_fields(state: Any) -> dict[str, str]:
    """Extract the canonical fields every provider builder consumes."""
    status = str(getattr(state, "status", "") or "")
    title = str(
        getattr(state, "job_title", "") or getattr(state, "job_id", "") or "BLAST job"
    )[:_MAX_TITLE]
    program = str(getattr(state, "program", "") or "")[:64]
    db = str(getattr(state, "db", "") or "")[:64]
    error_code = (
        str(getattr(state, "error_code", "") or "")[:_MAX_ERROR]
        if status == "failed"
        else ""
    )
    return {
        "status": status,
        "title": title,
        "program": program,
        "db": db,
        "error_code": error_code,
        "job_id": str(getattr(state, "job_id", "") or ""),
        "emoji": _EMOJI.get(status, "\u26a0\ufe0f"),
    }


def _summary_text(fields: dict[str, str]) -> str:
    lines = [f"{fields['emoji']} BLAST job *{fields['title']}* {fields['status']}"]
    meta = " \u00b7 ".join(p for p in (fields["program"], fields["db"]) if p)
    if meta:
        lines.append(meta)
    if fields["error_code"]:
        lines.append(f"error: {fields['error_code']}")
    return "\n".join(lines)


def _build_slack_payload(fields: dict[str, str], job_url: str) -> dict[str, Any]:
    """Slack Block Kit: header + a section of program/db/error + Open button."""
    header = f"{fields['emoji']} BLAST job {fields['status']}"
    section_text = f"*{fields['title']}*"
    detail_lines = []
    if fields["program"] or fields["db"]:
        detail_lines.append(
            "  ".join(
                [p for p in (f"`{fields['program']}`" if fields["program"] else "",
                             f"`{fields['db']}`" if fields["db"] else "") if p]
            )
        )
    if fields["error_code"]:
        detail_lines.append(f":warning: `{fields['error_code']}`")
    if detail_lines:
        section_text = section_text + "\n" + "\n".join(detail_lines)
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": section_text}},
    ]
    if job_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open in dashboard"},
                        "url": job_url,
                    }
                ],
            }
        )
    # ``text`` is the notification fallback Slack reads in mobile previews +
    # screen readers; the blocks are the rich render. Both required.
    return {"text": _summary_text(fields), "blocks": blocks}


def _build_teams_payload(fields: dict[str, str], job_url: str) -> dict[str, Any]:
    """Microsoft Teams legacy MessageCard (the format incoming webhooks accept).

    Note: Microsoft is sunsetting Office 365 Connectors in favour of the
    Workflows app, but classic incoming-webhook URLs still receive MessageCard
    today. When the connector finally retires, swap this for an Adaptive Card
    posted via the Workflows webhook; the dispatch contract here is unchanged.
    """
    facts: list[dict[str, str]] = []
    for label, value in (
        ("Program", fields["program"]),
        ("Database", fields["db"]),
        ("Error", fields["error_code"]),
    ):
        if value:
            facts.append({"name": label, "value": value})
    section: dict[str, Any] = {
        "activityTitle": f"**{fields['title']}**",
        "activitySubtitle": fields["status"],
    }
    if facts:
        section["facts"] = facts
    payload: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": _COLOR_HEX.get(fields["status"], "808080"),
        "summary": f"BLAST job {fields['status']}",
        "title": f"{fields['emoji']} BLAST job {fields['status']}",
        "sections": [section],
    }
    if job_url:
        payload["potentialAction"] = [
            {
                "@type": "OpenUri",
                "name": "Open in dashboard",
                "targets": [{"os": "default", "uri": job_url}],
            }
        ]
    return payload


def _build_discord_payload(fields: dict[str, str], job_url: str) -> dict[str, Any]:
    """Discord embeds: title + fields + colour."""
    embed_fields: list[dict[str, Any]] = []
    for label, value in (
        ("Program", fields["program"]),
        ("Database", fields["db"]),
        ("Error", fields["error_code"]),
    ):
        if value:
            embed_fields.append({"name": label, "value": value, "inline": True})
    embed: dict[str, Any] = {
        "title": f"{fields['emoji']} {fields['title']}",
        "description": f"BLAST job {fields['status']}",
        "color": _COLOR_INT.get(fields["status"], 0x808080),
    }
    if embed_fields:
        embed["fields"] = embed_fields
    if job_url:
        embed["url"] = job_url
    return {
        "content": _summary_text(fields),
        "embeds": [embed],
    }


def build_message(state: Any, *, url: str = "") -> dict[str, Any]:
    """Build a provider-aware webhook body for a terminal job.

    ``url`` is the target webhook URL — it is used **only** to detect the
    provider (Slack / Teams / Discord) so the right rich format is sent. An
    unknown / Logic Apps / empty URL produces the generic ``{text, content}``
    shape, preserving backward compatibility with the previous behaviour.
    """
    fields = _message_fields(state)
    job_url = _resolve_job_url(fields["job_id"])
    provider = detect_provider(url)
    if provider == "slack":
        return _build_slack_payload(fields, job_url)
    if provider == "teams":
        return _build_teams_payload(fields, job_url)
    if provider == "discord":
        return _build_discord_payload(fields, job_url)
    # generic / logic-apps: keep the legacy two-key shape so a custom subscriber
    # parsing ``text`` or ``content`` continues to work.
    text = _summary_text(fields)
    body: dict[str, Any] = {"text": text, "content": text}
    if job_url:
        body["job_url"] = job_url
    return body


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
            # Surface rate-limit hints so an operator can correlate a 429 burst
            # with their workload. Body intentionally not logged (PII / token risk).
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                LOGGER.warning(
                    "webhook post rate-limited (429); Retry-After=%s", retry_after
                )
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
        if post_webhook(config.url, build_message(state, url=config.url)):
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
