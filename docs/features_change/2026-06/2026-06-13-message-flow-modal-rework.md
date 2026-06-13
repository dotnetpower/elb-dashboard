---
title: Message Flow modal — wider layout, textless broker tiles, click-through job modal, consumer dedup
description: The Service Bus Message Flow modal now uses the full screen width, renders broker jobs as compact textless tiles with hover tooltips, opens job JSON in its own modal instead of an inline panel, and deduplicates consumer cluster cards so a single cluster no longer appears twice.
tags:
  - ui
  - blast
---

# Message Flow modal rework

## Motivation

The expanded **Message Flow** modal wasted horizontal space (capped at 1080 px
with a single-column broker lane), the broker boxes overflowed their text, the
job JSON opened as an inline panel pushed below the lanes, and the Consumers
lane showed the *same* AKS cluster as two separate cards (e.g. `elb-cluster-01`
once with a resource group and once without). The duplicate cards read as
"garbage data" to operators.

## User-facing change

- **Wider modal** — `maxWidth` 1080 → 1440 px; the three lanes now live in a
  responsive `.message-flow-lanes` grid (Broker gets the most room) that stacks
  to a single column below 900 px wide.
- **Textless broker tiles** — each in-flight job is a compact tile whose width
  is proportional to query length and whose tint is the submitter's color.
  Queued tiles render dimmer than running ones. All per-job detail (program,
  query size, status/phase, db, submitter, cluster) is shown on **hover via a
  native tooltip** instead of cramped inline text.
- **Click-through job modal** — clicking a tile now opens a dedicated job-detail
  modal (its own backdrop, on top of the flow modal) with a compact summary
  plus the redacted JobState JSON, replacing the old inline JSON panel. Escape
  closes the detail modal first, then the flow modal.
- **Consumer dedup** — the Consumers lane groups by cluster *name* instead of
  the `(subscription, resource group, name)` triple, so a cluster whose
  resource group / subscription were not yet backfilled on a queued row no
  longer splits into two cards, and not-yet-placed jobs collapse into a single
  `unassigned` card. The broker tile cap rose 60 → 120 (tiles are tiny now).

## "elb-cluster-01 appears twice" — root cause

The Consumers lane reflects **active `JobState` rows** (status `queued` /
`running`), grouping them by the cluster they target. The previous key was the
full `(subscription_id, resource_group, cluster_name)` triple, so the same
logical cluster appeared as two cards whenever some rows carried the rg/sub and
others (queued before placement) did not. Grouping by name fixes the duplicate.

Note: if a cluster genuinely no longer exists but still has `queued`/`running`
JobState rows, the card honestly reflects those *stale* rows — that is a data
reconciliation concern, not a visualization bug, and is intentionally **not**
hidden (hiding active rows could mask genuinely stuck jobs).

## API / IaC diff summary

- Backend: `api/services/message_flow.py` — consumer grouping keyed by cluster
  name with rg/sub backfill; `_MAX_BROKER_BOXES` 60 → 120. No response field
  added or removed (the `consumers.clusters[]` shape is unchanged).
- Frontend: `web/src/components/cards/MessageFlow/MessageFlowModal.tsx` — modal
  width, textless tiles + tooltip, nested `JobDetailModal`, responsive grid
  class. `web/src/theme/glass.css` — `.message-flow-lanes` grid + media query.
- No Bicep / infra change.

## Validation

- `uv run pytest -q api/tests/test_message_flow.py api/tests/test_route_contracts.py`
  → 14 passed (incl. new `test_consumers_dedup_same_cluster_when_rg_sub_backfilled`).
- `cd web && npx eslint src/components/cards/MessageFlow/MessageFlowModal.tsx` → clean.
- `cd web && npm run build` → built, no type errors.
