---
title: "Message Flow — Service Bus telemetry footer + scope/truncation badges"
description: "Adds real Service Bus admin telemetry (queue size %, transfer counters, status, rolling DLQ-growth delta) to the Message Flow modal footer; promotes the inline footer to an SRP-isolated ServiceBusTelemetryPanel; and adds scope / truncation badges and a sub-second-drain tooltip so the constellation stops looking contradictory with the raw queue depth."
tags:
  - user-guide
  - blast
  - architecture
---

# Message Flow — Service Bus telemetry footer + scope/truncation badges

## Motivation

The Message Flow card is misnamed in a subtle but real way: it draws active
BLAST **jobs**, not Service Bus **messages**. The request queue drains in well
under a second on every observed deployment, so `active_message_count` is
essentially always zero, while the constellation shows several in-flight nodes.
The two number sets look contradictory unless the operator already knows the
backing model.

The existing inline footer surfaced only four raw counters
(`active_message_count`, `scheduled_message_count`, `dead_letter_message_count`,
plus a "completions topic: …" trailer when configured) and offered no
growth-rate hint for DLQ, no queue size %, no entity status, and no
transfer-path visibility. The header had no scope indicator (own jobs vs. all
submitters under
`BLAST_SHARED_VISIBILITY=true`) and no truncation indicator when the broker
list was capped or the JobState read window was hit — both invisible failure
modes.

This is the P0 slice (items 1, 2, 6, 7, 10, 17, 30 of the 38-item review)
implemented with a Single-Responsibility split on both sides.

## User-facing change

- **Modal header** now carries:
  - a **scope badge** — `Your jobs only` (default) or `All submitters` (warning
    tone) with a tooltip explaining the `BLAST_SHARED_VISIBILITY` deployment
    flag.
  - a **truncated badge** (warning border) whenever the broker box cap is hit
    OR the JobState read window is hit, with a tooltip that says which one and
    the exact `active_shown` / total numbers.
  - a `help`-cursored tooltip on the `{N} active` indicator explaining the
    sub-second drain so the zero-count is no longer surprising.
- **Modal footer** is now the new SRP-isolated `ServiceBusTelemetryPanel`. In
  addition to the four old counters it now shows, when the data is available:
  - queue **size** (B/KB/MB/GB) and **size %** of `max_size_in_mb`, tinted
    warning/danger at 50% / 80% (the same thresholds the Storage card uses).
  - queue **status** dot + label (`active`, `disabled`, `sendDisabled`, …).
  - **transfer / DLQ-of-transfer** counts summed across the optional completion
    topic's subscriptions, surfaced only when non-zero.
  - a **DLQ growth** row showing the rolling-window delta:
    `+N in last Ns` (warning), `no growth in last Ns` (muted), or
    `N since first sample` (honest text on the first poll because the in-process
    rolling window has nothing to compare against yet).
- **Dashboard card title** now reads `Message Flow · active jobs` with a
  tooltip clarifying it is the work in flight on AKS, not raw queue depth.

The card still **hides entirely** when the integration is off (unchanged
default experience).

## API / IaC diff summary

### Backend (`api/`)

The backend telemetry fields (`queue.telemetry.{size_in_bytes, max_size_in_mb,
size_pct, transfer_message_count, transfer_dead_letter_message_count, status,
created_at, updated_at, accessed_at}`, per-subscription `transfer_*` counters,
and the snapshot's `dlq_delta` block) already shipped in the prior commit; this
change only carries the small ruff-cleanup follow-up:

- [api/services/service_bus.py](../../../api/services/service_bus.py)
  — replaces an obsolete `# noqa: BLE001` with an explanatory comment on the
  best-effort timestamp helper.
- [api/services/message_flow.py](../../../api/services/message_flow.py)
  — same cleanup on the `record_dlq_sample` exception handler.

`uv run pytest -q api/tests` stays at 3562 passing, including the existing
8 `service_bus_telemetry` tests, 3 `service_bus.entity_counts` telemetry
tests, and 2 `message_flow` dlq_delta integration tests.

### Frontend (`web/`)

This is where the user-visible change lands — the backend fields were
otherwise dormant because no consumer rendered them.

- [web/src/api/messageFlow.ts](../../../web/src/api/messageFlow.ts)
  — adds `MessageFlowDlqDelta` interface and an optional
  `dlq_delta?: MessageFlowDlqDelta | null` field on `MessageFlowSnapshot`.
- [web/src/api/settings.ts](../../../web/src/api/settings.ts)
  — extends `ServiceBusCounts.queue` with an optional `telemetry?` block and
  adds optional `transfer_message_count?` / `transfer_dead_letter_message_count?`
  on each subscription entry. All additive.
- [web/src/components/cards/MessageFlow/serviceBusTelemetryFormat.ts](../../../web/src/components/cards/MessageFlow/serviceBusTelemetryFormat.ts)
  (NEW) — pure helpers (`formatBytes`, `formatPct`, `fillTone`, `statusTone`,
  `dlqDeltaSummary`) extracted out of the panel so the math is unit-testable.
  The `formatPct` and `fillTone` helpers lock down the **percent vs fraction
  contract**: backend ships `size_pct` on the 0..100 scale, so a value of
  `0.05` means 0.05 %, not 5 %. (Caught and fixed in self-review.)
- [web/src/components/cards/MessageFlow/serviceBusTelemetryFormat.test.ts](../../../web/src/components/cards/MessageFlow/serviceBusTelemetryFormat.test.ts)
  (NEW) — 14 helper unit tests, including a dedicated regression guard for
  the percent-scale contract.
- [web/src/components/cards/MessageFlow/ServiceBusTelemetryPanel.tsx](../../../web/src/components/cards/MessageFlow/ServiceBusTelemetryPanel.tsx)
  (NEW) — SRP-isolated pure presentation panel. No fetch, no state, no
  in-line math; just JSX over the snapshot and the format helpers.
- [web/src/components/cards/MessageFlow/MessageFlowModal.tsx](../../../web/src/components/cards/MessageFlow/MessageFlowModal.tsx)
  — replaces the inline footer with `<ServiceBusTelemetryPanel snapshot={…} />`,
  adds scope + truncation badges to the header, and adds the sub-second-drain
  tooltip on the active count.
- [web/src/components/cards/MessageFlow/MessageFlowCard.tsx](../../../web/src/components/cards/MessageFlow/MessageFlowCard.tsx)
  — adds the `active jobs` subtitle and a title tooltip clarifying scope.

### IaC

None. Pure code change; no Bicep / Container App template touches; no new
environment variables or secrets; same Service Bus permissions; no Storage /
ACR / AKS changes.

## SRP impact

- **Backend split (already in place)**: the SDK wrapper (`service_bus.py`)
  stays a thin wrapper over `azure.servicebus`. All time-aware / derived state
  lives in `service_bus_telemetry.py`. Adding a second time-series (e.g. queue
  size trend) lands as a function in that module, not as another branch in
  `entity_counts()`.
- **Frontend split (this change)**: the modal owns layout, focus, escape
  handling, data fetching, and JSON detail flow. The new panel owns *only*
  presentation of the raw Service Bus metrics. The format helpers (size,
  percent, tone thresholds, DLQ summary) live in their own pure module with
  their own unit tests. Adding a third telemetry tile (e.g. consumer lag)
  edits the panel; changing a format rule edits the helper module — neither
  touches the 525-line modal body.

## Validation evidence

- Backend wide: `uv run pytest -q api/tests` → **3562 passed, 3 skipped**.
- Backend lint: `uv run ruff check api` → all checks passed.
- Frontend focused: `npx vitest run src/components/cards/MessageFlow/`
  → 44 passed (4 files, including the 14 new helper tests).
- Frontend wide: `npx vitest run` → **884 passed (98 files)**.
- Frontend build: `npm run build` → built successfully (no new TS errors).
- Docs frontmatter guard: `uv run python scripts/docs/check_frontmatter.py`
  → OK.
- Consumer audit: searched `MessageFlowSnapshot`, `ServiceBusCounts`,
  `sb_counts`, `messageFlowApi` across `web/src/` — every consumer is happy
  with the additive shape (every new field is optional). The only other
  consumer of `ServiceBusCounts` (`ServiceBusSection.tsx` in Settings) only
  reads the four legacy counters; it ignores the new optional fields and
  keeps rendering identically.
- Backward compat: an older snapshot (no `telemetry`, no `dlq_delta`) renders
  identically to before — the panel falls through every `?.` and the new badges
  default to "Your jobs only" with no truncation pill.
- Self-review caught a unit bug: `size_pct` is on the 0..100 scale, not 0..1.
  Fixed in the helper extraction and locked down with a dedicated test.
