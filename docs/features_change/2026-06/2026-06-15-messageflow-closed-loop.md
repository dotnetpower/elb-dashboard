---
title: MessageFlow closed-loop visualization (Queue / Topic split + completion loop)
description: >-
  The Service Bus MessageFlow constellation is reworked from the single
  "Bounded Lanes" broker into a four-stage closed loop — Actors to a Queue box,
  to Workers (queue consumers), to a Topic box — with the completion looping
  back over the top to the submitting actor, so a submitter reads as both a
  producer and a completion subscriber.
tags:
  - ui
---

# MessageFlow closed-loop visualization

## Motivation

The previous "Bounded Lanes (A1)" constellation drew a single bordered broker
region (a Queue lane stacked above a Topic lane) between Producers and
Consumers. Two problems surfaced while reviewing the live picture:

1. **Queue and Topic read as one stage.** Stacking the two Service Bus entities
   inside one box made them look like a single broker step rather than the two
   distinct entities they are (the request queue vs. the completion topic).
2. **A submitter's dual role was invisible.** The same submitter (e.g. an
   `svc-batch` API client) both *produces* to the request queue and *receives*
   the completion of its own jobs. The left-to-right layout pinned every
   submitter in the "Producers" column, so it looked like producers only ever
   publish requests — the completion coming back to them was never shown.

## User-facing change

The MessageFlow modal now renders the **"Closed Loop (A4)"** constellation:

- **Four stages**: **Actors** (left) → **Queue box** → **Workers** (the queue
  consumers / AKS clusters) → **Topic box** (right). The single broker box is
  split into a dedicated Queue box and a dedicated Topic box, each labelled with
  its Service Bus entity name.
- **Closed completion loop**: when a submitter has completed (settling) jobs, a
  faint dashed arc sweeps over the top from the Topic box back to that actor
  (arrow pointing into the actor). This makes the dual role **structural** — a
  submitter is visibly both a producer (arrows out to the Queue) and a
  completion subscriber (the loop arc returning in). Hovering an actor brightens
  its own loop.
- **Dual-role label**: an actor that has completed work is labelled
  `producer + subscriber` (otherwise it carries no sub-label); the column
  captions gain sub-labels (`produce + subscribe`, `requests`,
  `queue consumers`, `completions`) to disambiguate the "consumer" term
  (queue consumer vs. topic subscriber).
- **System subscribers stay distinct**: the named Service Bus subscriptions
  (e.g. `dashboard` / `autostop` / `audit`) are grouped under a `SYSTEM SUBS`
  heading inside the Topic box, so an actor is never misread as owning one of
  those subscriptions.
- Jobs are force-positioned by lifecycle: queued → Queue box, running → between
  Queue and Workers, completed (settling) → Topic box, each clamped to its box.
- The travelling "energy" particle layer was **removed** in favour of the calm
  static A4 layout; in-flight state is conveyed by the node glyphs (queued ring,
  running halo, completed check) and the completion loop, not moving dots.

## Honesty guardrails

- The completion loop maps a **real per-actor completion count** derived from
  `MessageFlowBox.lifecycle === "settling"` (claim-check pattern). It is **not**
  a claim that the submitter owns a named Service Bus subscription — those are
  the system subscriptions, shown separately in the Topic box.
- No new backend fields or API contract changes: the new view is derived
  entirely from the existing `MessageFlowSnapshot` (`producers`, `broker`,
  `consumers.clusters`, `sb_counts.subscriptions`).

## API / IaC diff summary

None. Pure frontend presentation change over the existing
`GET /monitor/message-flow` snapshot. Files touched:

- `web/src/components/cards/MessageFlow/MessageFlowConstellation.tsx` — 4-stage
  geometry, Queue/Topic boxes, completion-loop layer + arrowhead, dual-role
  label, particle system removed, tick clamp per box.
- `web/src/components/cards/MessageFlow/MessageFlowModal.tsx` — caption +
  docstring updated to the closed-loop terminology.
- `web/src/components/cards/MessageFlow/MessageFlowCard.tsx` — docstring.
- `web/src/theme/glass.css` — new `.mf-col-sublabel` style.

The closed-loop layout was prototyped in a standalone D3 mockup during design
review and then ported onto the live snapshot; the throwaway mockup files were
removed once the component landed.

## Validation

- `npm run build` (tsc -b + vite) — clean.
- `npx vitest run src/components/cards/MessageFlow` — 44 passed (4 files).
- `npx eslint src/components/cards/MessageFlow` — clean.
- `get_errors` on the rewritten component — no errors.
- Design approved against a standalone D3 closed-loop mockup during review
  (the component ports that exact layout/loop logic onto the live snapshot).
- Live Service Bus render: to be eyeballed on a deployment with the integration
  enabled (the component renders nothing when the integration is off).
