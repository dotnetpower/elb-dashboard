---
title: Message Flow — actor label fix, queued/running clarity, DLQ footer cleanup
description: Stop the actor labels clipping off the SVG edge, make queued vs running visually distinct, and remove the misleading DLQ-growth row from the Service Bus telemetry footer.
tags:
  - ui
  - blast
---

# Message Flow — actor label clipping fix + queued/running clarity

## Motivation

On a narrower Message Flow modal the left-most **Actors** column labels
(`servicebus ·api (30)`, long UPN submitters) were rendered left-anchored at a
fractional x position (`w * 0.1`) and ran off the left edge of the SVG, so the
label appeared to start mid-word (e.g. only `ebus ·api (30)` was visible).

Separately, queued and running jobs were hard to tell apart: queued jobs were
drawn as fully transparent (hollow) dots, so the swarm of waiting work inside
the Queue box was nearly invisible, and the only numeric summary in the header
lumped everything together as "N active".

## User-facing change

- **Actors labels no longer clip.** Actors now sit on a fixed minimum left
  gutter (`min(max(w*0.12, 128), w*0.2)`) so their labels always fit; on a wide
  modal the fractional position still dominates. The redundant ` ·api` suffix is
  dropped from the actor label (the square-vs-circle glyph already encodes api
  vs user), keeping labels narrow.
- **Queued vs running is clearer.**
  - Queued/pending dots keep a soft submitter-tone fill instead of being fully
    transparent, so the waiting swarm in the Queue box is visible; the dashed
    ring plus the absence of the running halo still marks them as "waiting".
  - The **Queue** column sub-label now reads `N queued` and the **Workers**
    column sub-label reads `N running` whenever there is live work, so the split
    is legible without decoding dot styling.
  - The legend's "queued" swatch gains the same faint fill so it matches the
    graph.

## API / IaC diff summary

None. Pure presentation change over the existing `MessageFlowSnapshot`; no
backend, schema, or infra change. `N queued` / `N running` are derived from the
already-shipped `broker[]` boxes.

## Telemetry footer — DLQ number cleanup

### Motivation

The Service Bus telemetry footer showed three DLQ-related figures and one of
them was misleading. Two are real Azure runtime counters and stay:

- `queue DLQ N` — request-queue dead-letter depth (managed by the `dlq_cleanup`
  task).
- `completions topic … DLQ N` — the completion topic subscriptions' dead-letter
  depth.

The third, the **"DLQ growth"** row, was an in-process rolling-window estimate
that (a) resets on every api-sidecar restart, and (b) tracks *only* the small
request-queue DLQ while the eye-catching number is the completion-topic DLQ —
so it read as contradictory garbage next to the real counters.

### User-facing change

- Removed the "DLQ growth" row from the telemetry footer.
- Relabelled the request-queue dead-letter figure `DLQ N` → `queue DLQ N` and
  updated its tooltip so it is unambiguous against the completion-topic
  `… DLQ N` figure. Both remaining numbers are the real Azure counters,
  unchanged.

### API / IaC diff summary

Frontend-only. The backend still ships the additive `dlq_delta` field on the
snapshot (harmless, no extra admin call — it is recorded during the counts read
that already runs); it is simply no longer rendered. No purge of any Service Bus
message was performed — the completion-topic backlog is real customer data and
is out of scope for this UI change. Removed the now-dead `dlqDeltaSummary`
helper + its `DlqDeltaSummary` interface and unit tests.

## Validation evidence

- `cd web && npm run build` — clean (built in ~3.6s, no TS errors).
- `npx vitest run src/components/cards/MessageFlow` — 44 tests pass across 5
  files after removing the `dlqDeltaSummary` block.
- No unit/e2e assertions reference the ` ·api` suffix, the column sub-label
  strings, `dlqDeltaSummary`, or `sb-dlq-delta` (`grep` over `web/src/**`,
  `scripts/e2e/**`, and the message-flow ui-mock spec).
