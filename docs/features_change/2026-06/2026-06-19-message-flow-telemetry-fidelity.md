---
title: Message Flow telemetry fidelity + job-detail redaction hardening
description: The Message Flow card now surfaces the completion topic's subscription backlog (pending + DLQ depth) instead of just the topic name, clarifies that the job-detail "Query size" is input length only, hardens the raw-JSON inspector to redact subscription IDs and SAS signatures (charter §12), and fixes a stale size_pct type-doc that claimed a 0..1 fraction when the value is on the 0..100 scale.
tags:
  - ui
  - architecture
---

# Message Flow telemetry fidelity + job-detail redaction hardening

## Motivation

A review of the values shown when an operator clicks queues/topics in the
**Message Flow** card surfaced a set of fidelity and sanitisation gaps. Several
of the original critique items turned out to already be handled (the broker-box
truncation flag `broker_truncated`/`read_truncated` is rendered, and the peeked
queue-message section already labels itself "peeked, not removed"), so this
change implements only the genuinely-missing improvements after re-verification.

## User-facing change

- **Completion topic backlog (was: name only).** The telemetry footer showed
  `completions topic: <name>` with no health signal. It now appends the
  backlog across the topic's subscriptions — `<N> pending` (completion messages
  not yet consumed) and `· DLQ <M>` (completions that dead-lettered, warning
  tone) — using the `subscriptions[].active_message_count` /
  `dead_letter_message_count` the backend already returns. An operator can now
  tell whether results are actually draining, not just that a topic exists.
- **"Query size" clarified.** The job-detail modal's `Query size` now carries a
  tooltip stating it is the input sequence length only — not a measure of job
  runtime or cost (a core_nt search is dominated by the database, not the query
  length), so the box-size visual is not misread as a cost proxy.
- **Raw-JSON inspector redaction hardened (charter §12).** Clicking a broker box
  renders the raw JobState JSON. Redaction was a 2-key denylist
  (`owner_oid`, `tenant_id`); it now also drops `subscription_id` and SAS/token
  keys, and scrubs the `sig=` signature out of any SAS-bearing URL string at any
  nesting depth, so the inspector can never echo a subscription ID or a live SAS
  credential. The submitter `owner_upn` alias stays visible (shown intentionally
  elsewhere). The logic moved to a pure, unit-tested `redactJobJson` module.

No behaviour change to the flow graph, polling, or counts.

## API / IaC diff summary

Frontend only:

- `web/src/components/cards/MessageFlow/redactJobJson.ts` (new) — extracted,
  hardened redaction + `redactJobJson.test.ts` (5 tests).
- `web/src/components/cards/MessageFlow/MessageFlowModal.tsx` — use the new
  module; add the Query-size tooltip.
- `web/src/components/cards/MessageFlow/ServiceBusTelemetryPanel.tsx` — render
  the completion topic's subscription pending + DLQ depth.
- `web/src/api/settings.ts` — fix the stale `size_pct` doc (it is the 0..100
  percent the backend ships, e.g. `50.0` = 50 %, not a 0..1 fraction; the
  renderer was already correct).

No backend, no API, no IaC change.

## Validation evidence

- `npx vitest run src/components/cards/MessageFlow/` — 49 passed (incl. 5 new
  `redactJobJson` tests covering sensitive-key drop at depth, SAS `sig=` scrub,
  `owner_upn` preserved, arrays/primitives).
- `npm run build` — type + bundle clean.
- `npx eslint` on the changed files — clean.

## Deferred (need a backend signal or their own design — not implemented here)

These critique items are real but require backend support to do without adding
misleading UI, and are left as follow-ups:

- Distinguish "telemetry partial/degraded" from "empty/healthy" (needs a backend
  `degraded_fields` signal; today both render em-dashes).
- Persist the DLQ-growth rolling window (currently in-process memory, resets on
  revision restart) or compute it from Azure Monitor.
- A jobstate-active vs Service-Bus-queue-depth drift badge.
- A "show raw" toggle / curated-field default for the job-detail JSON.
- An inline DLQ inspect/drain affordance (the `servicebus dlq_cleanup` task
  exists but is not surfaced in the card).
