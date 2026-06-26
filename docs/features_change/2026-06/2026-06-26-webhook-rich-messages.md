---
title: Provider-aware rich webhook messages (Slack / Teams / Discord)
description: Webhook notifications now build a provider-aware rich payload (Slack Block Kit, Teams MessageCard, Discord embed) with a deep-link to the job in the dashboard, while keeping the legacy {text, content} shape as the generic fallback so custom Logic Apps integrations are unbroken.
tags:
  - operate
  - blast
---

# Provider-aware rich webhook messages (Slack / Teams / Discord)

## Motivation

The opt-in webhook channel shipped a plain `{text, content}` payload — readable
by all three major providers but visually flat (no status colour, no
"Open in dashboard" button, no field layout). This pass routes each terminal-job
notification through a provider-aware builder so the message renders natively
where the operator reads it, **without** breaking the existing generic fallback
that custom Logic Apps subscribers parse.

## User-facing change

Webhook messages posted by the dashboard now render as:

| Provider | Format | Render |
| --- | --- | --- |
| Slack | Block Kit (header + section + actions button) | Status header, title + program/db, **Open in dashboard** button |
| Teams (incoming webhook) | Legacy MessageCard | Coloured banner, status fact row, **Open in dashboard** action |
| Discord | Embed + content | Coloured embed with title + program/db/error fields |
| Logic Apps / other allowlisted | `{text, content}` legacy shape | Unchanged — preserves backward compatibility with custom integrators |

The deep-link is `{DASHBOARD_PUBLIC_URL}/blast/jobs/{job_id}` resolved through
the existing `api.services.control_plane_url.resolve_control_plane_url()` (env
override → operator-configured custom domain → Container App FQDN → empty). When
no base URL can be resolved the message is sent **without** the button rather
than embedding a broken link.

The Settings → Webhooks **Send test** button now sends the same rich payload
shape, so an operator sees the actual rendered look in their tool.

## Design

* `detect_provider(url)` classifies the host against the same SSRF allowlist
  (`hooks.slack.com` / `*.webhook.office.com` / `*.webhook.office365.us` /
  `discord.com` / `discordapp.com`); everything else (including
  `*.logic.azure.com` and `WEBHOOK_ALLOWED_HOSTS` extras) falls through to
  generic.
* One builder per provider — `_build_slack_payload`, `_build_teams_payload`,
  `_build_discord_payload`, plus the legacy `{text, content}` for generic. A
  single colour palette + emoji map drives all three so the status visual is
  consistent.
* `build_message(state, *, url="")` keeps the old single-arg call working
  (returns generic) — only `dispatch_job_webhooks` and the `Send test` endpoint
  pass the URL.

### Hardening

* Title is capped at 240 chars and `error_code` at 200 chars before going into
  any payload, so a runaway error string cannot blow past provider field limits.
* `post_webhook` now logs the `Retry-After` header on a `429` rate-limit
  response (one-liner WARNING, no body) so an operator can correlate a 429
  burst with their job workload.
* No payload field is ever populated with a SAS token or subscription id —
  message content is derived strictly from `JobState` columns the dashboard UI
  already shows.

### Trade-off: Microsoft Teams Connector deprecation

Microsoft is sunsetting **Office 365 Connectors** (the classic incoming-webhook
endpoint that accepts MessageCard) in favour of the **Workflows app**, which
takes Adaptive Cards via its own webhook endpoint. Today the dashboard's
allowlist still accepts the classic `*.webhook.office.com` host, and existing
operator subscriptions there continue to receive MessageCard. When the connector
finally retires, the dispatch contract here is unchanged: swap `_build_teams_payload`
for an Adaptive Card builder pointed at the Workflows webhook URL the operator
will configure. The change note will live alongside this one when the migration
ships.

## API / IaC diff summary

* `api/tasks/webhooks.py`: `build_message` gains an optional `url=` kwarg and a
  provider switch; new helpers `detect_provider`, `_resolve_job_url`,
  `_message_fields`, `_summary_text`, `_build_slack_payload`,
  `_build_teams_payload`, `_build_discord_payload`. `post_webhook` logs 429
  `Retry-After`.
* `api/routes/settings/webhooks.py`: `/test` now calls `build_message` with the
  configured URL so the test render matches the real notification shape.
* `docs/operate/feature-gates.md`: `WEBHOOK_NOTIFICATIONS_ENABLED` description
  updated to reflect the provider-aware payload + deep link.
* No new dependency, no IaC change, no new gate.

## Validation evidence

* `uv run pytest -q api/tests/test_webhooks_task.py api/tests/test_settings_webhooks.py api/tests/test_webhooks_pref.py` — 37 passed (provider detection, Slack Block Kit shape, Teams MessageCard shape + colour + action URL, Discord embed shape, generic backward-compat, missing-base-URL no-button, generic-test message + sweep + cap unchanged).
* `uv run ruff check api` — all checks passed.
* `uv run pytest -q api/tests` — full suite (will be re-run in the commit step).
