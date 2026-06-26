---
title: Outbound webhook notifications (Slack/Teams/Discord, opt-in)
description: A default-OFF beat sweep that POSTs a Slack/Teams/Discord-compatible message to an SSRF-validated webhook URL when a BLAST job reaches a terminal state, plus a Settings panel to configure and test it.
tags:
  - operate
  - ui
---

# Outbound webhook notifications (Slack/Teams/Discord, opt-in)

## Motivation

The in-app notification center (bell) only helps while the dashboard is open.
This adds the "results come to you" channel from the ops-readiness checklist:
post a message to a Slack / Teams / Discord incoming webhook when a job finishes,
so a researcher does not have to watch the dashboard.

## User-facing change

* A new **Settings → Webhooks** section lets an operator set a webhook URL, an
  enable toggle, and the scope (all terminal jobs / failed only), plus a **Send
  test** button.
* When `WEBHOOK_NOTIFICATIONS_ENABLED` is on AND a webhook is configured, a beat
  sweep POSTs `✅/❌/⏸️ BLAST job <title> <status>` to the webhook for each newly
  terminal job.
* The URL is a secret — it is stored masked and only ever returned masked.

## Design

* **Dispatch = beat sweep** (consistent with the derived-view notification
  center, not a write-time hook): `dispatch-job-webhooks` scans recent terminal
  jobs (`list_recent_terminal`, 24h window, capped) and POSTs any without a
  per-job `_webhook_sent` marker, then marks them. This catches **every**
  terminal transition (local submit / OpenAPI webhook / K8s refresh) without
  three separate hooks.
* **SSRF guard** (`webhooks_pref.validate_webhook_url`): https-only, IP-literal
  hosts rejected, host must be under an allowlist (`hooks.slack.com` /
  `*.webhook.office.com` / Discord / Logic Apps; extend with
  `WEBHOOK_ALLOWED_HOSTS`). Validated on **save** AND **at send time** (in case
  the allowlist tightened after save).
* **Default-OFF gate** `WEBHOOK_NOTIFICATIONS_ENABLED` (charter §12a Rule 4) — no
  outbound call until an operator opts in. Documented in
  [feature-gates.md](feature-gates.md).
* Message is `{text, content}` so the single payload works for Slack + Teams
  (`text`) and Discord (`content`).

### Safety (critique + hardening)

* **Blank URL = keep current** (High finding): the URL is shown masked, so an
  operator toggling enabled / changing scope must not be forced to re-enter the
  secret. A blank URL on save keeps the stored one; to stop notifications, set
  `enabled=False`.
* Delivery is **at-least-once**: a POST that succeeds but whose marker write
  fails may re-send next sweep — acceptable for a notification.
* The webhook URL is stored in the platform Table (Storage RBAC + private
  endpoint), not Key Vault — same pattern as other per-deployment prefs; it is
  never returned to the browser unmasked.

## API / IaC diff summary

* New backend: `api/services/webhooks_pref.py` (config + SSRF guard + mask),
  `api/tasks/webhooks.py` (beat sweep + Slack/Teams/Discord payload),
  `api/routes/settings/webhooks.py` (`GET/PUT /api/settings/webhooks`, `POST
  …/test`), `JobStateRepository.list_recent_terminal`, beat entry +
  task-discovery in `api/celery_app.py`.
* New frontend: `web/src/api/webhooks.ts` (+ barrel),
  `web/src/components/settings/sections/WebhookSection.tsx`, a Webhooks tab in
  `SettingsPanel.tsx`.
* No IaC change (table created on first use), no new dependency (uses `httpx`),
  no secret in env. `test_tasks_facade_contract` updated for the new task's
  string monkeypatch target.

## Validation evidence

* `uv run pytest -q api/tests/test_webhooks_pref.py api/tests/test_webhooks_task.py api/tests/test_settings_webhooks.py` — 31 passed (SSRF allow/deny matrix, IP-literal + suffix-trick rejection, masking, blank-keeps-existing, sweep send/skip/filter/cap, gate off, route 400/test).
* `uv run ruff check api` — all checks passed.
* `cd web && npm run build` — built successfully.
* `uv run pytest -q api/tests` — **4672 passed, 3 skipped, 0 failed** (the
  previously pre-existing STORAGE_DATE_LAYOUT failure was fixed earlier this
  session; the facade-contract guard was updated for the new task).
