---
title: Message Flow modal — accessibility, affordance, legend, and live-state UX pass
description: A UI/UX hardening pass on the Service Bus Message Flow modal — textless broker tiles gain a non-text status dot and real hover/focus affordance, queued state uses a dashed border instead of low opacity (AA contrast), a legend explains the color/width/status encodings, hovering a submitter highlights its broker tiles, the header shows a live "updated N ago" badge, and a caption clarifies broker boxes (jobs) vs Service Bus queue depth.
tags:
  - ui
  - blast
---

# Message Flow modal — UX hardening pass

## Motivation

A self-review of the reworked Message Flow modal surfaced several UI/UX gaps:
all per-job detail lived only in a hover `title` tooltip (invisible to touch /
keyboard users), queued tiles were signalled with `opacity: 0.6` (fails AA
contrast and reads as "disabled"), a declared `transition` had no `:hover` rule
so the clickable affordance was dead, there was no legend for the three visual
encodings (color = submitter, width = query length, fill = status), the
Producers → Broker → Consumers "flow" was only inferrable from color, and the
modal gave no signal that its data was live.

## User-facing change

- **Non-text status dot** on every broker tile (solid = running, ring = queued)
  so status survives where the hover tooltip cannot (touch, keyboard).
- **Queued tiles use a dashed border** instead of reduced opacity — AA contrast
  preserved, no "disabled" misread.
- **Real hover/focus affordance** — tiles lift on hover and show a focus ring on
  `:focus-visible` (keyboard navigable), via a new `.message-flow-box` class.
- **Legend bar** above the lanes explaining running/queued, width = query
  length, and color = submitter.
- **Producer ↔ broker highlight** — hovering a submitter (in either lane) dims
  every unrelated broker tile, making the flow mapping visible without arrows.
- **Running-first ordering** in the broker lane so the busy part is scannable.
- **Live "updated N ago"** badge in the header (ticks via `useRelativeTime`);
  the long `namespace_fqdn` now ellipsis-truncates instead of overflowing.
- **Footer caption** clarifying that broker boxes are in-flight *jobs* while the
  Service Bus queue depth above drains sub-second (so the two number sets are
  not contradictory).
- **Empty Consumers placeholder** ("Awaiting placement…") when jobs are queued
  but not yet assigned to a cluster.
- Dashboard strip swaps the `▶` glyph for the lucide `ChevronRight` icon to
  match the rest of the iconography.

## API / IaC diff summary

- Frontend only. `MessageFlowModal.tsx` (status dot, dashed queued, hover
  highlight state, legend, live badge, caption, empty placeholder, running-first
  sort), `MessageFlowCard.tsx` (ChevronRight, passes `updatedAt`),
  `theme/glass.css` (`.message-flow-box*`, `.message-flow-producer`,
  `.message-flow-legend`). No backend, no Bicep, no API contract change.

## Validation

- `cd web && npx eslint src/components/cards/MessageFlow/*.tsx` → clean.
- `cd web && npx vitest run src/components/cards/MessageFlow/` → 9 passed.
- `cd web && npm run build` → built, no type errors.
