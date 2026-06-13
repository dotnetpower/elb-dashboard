---
title: MessageFlow constellation — force-directed Service Bus flow visualization
description: >-
  The Service Bus MessageFlow modal renders a d3 force-directed "Bounded Lanes"
  constellation — producers to a Queue/Topic broker to consumers — positioning
  live jobs by status and surfacing a submitter's session group on hover.
tags:
  - ui
---

# MessageFlow constellation visualization

## Motivation

The Service Bus MessageFlow modal previously lacked a spatial, at-a-glance view
of how jobs move through the broker. Operators could read the raw flow but not
*see* the producer to broker to consumer topology or which jobs belong to the
same submitter session.

## User-facing change

- The MessageFlow modal now renders **`MessageFlowConstellation`**, a d3
  force-directed graph ("Bounded Lanes / A1" design):
  - **Producers** (left) to a bordered **Broker** region (Queue lane above a
    Topic lane) to **Consumers** (right).
  - Jobs are force-positioned by status: `queued` settle into the Queue lane,
    `running` into the broker centre.
  - Producers are api-dominant (rounded-square glyph) with the occasional human
    user (circle). Connection lines thin and fade with message age.
  - Hovering a submitter surfaces its **session group** (jobs sharing a
    submitter alias) and dims the rest. Clicking a job opens the existing JSON
    detail modal via `onSelectBox`.
- The component is a **pure presentation** over the live `MessageFlowSnapshot`.
  It never fabricates data: a "session" is just the set of active jobs sharing a
  submitter alias, and a missing field (query size, `created_at`) degrades the
  visual (minimum radius, neutral link age) instead of inventing a value. The
  empty state is owned by the parent modal.

## API / IaC diff summary

- New `web/src/components/cards/MessageFlow/MessageFlowConstellation.tsx`
  (presentation) + `constellationModel.ts` (pure layout/grouping model) +
  `constellationModel.test.ts`.
- `MessageFlowModal.tsx` renders the constellation; `web/src/theme/glass.css`
  adds the `.mf-*` styles.
- `web/vite.config.ts` adds a `vendor-d3` manual chunk; `web/package.json` adds
  `d3-drag` / `d3-force` / `d3-selection` (+ `@types/*`).
- No backend / IaC change.

## Validation evidence

- `cd web && npm test -- --run src/components/cards/MessageFlow/` — passing
  (`constellationModel.test.ts`, 13 tests).
- `cd web && npm run build` — clean (`vendor-d3` chunk emitted).
