---
title: E2E coverage for MessageFlow constellation + harden cluster-row scenarios
description: >-
  Add ui-mock e2e coverage for the MessageFlow constellation (card -> modal ->
  d3 graph -> job JSON detail, including the owner/tenant GUID redaction) and
  repair three pre-existing cluster-row / settings scenarios that drifted from
  the current UI.
tags:
  - testing
  - ui
---

# E2E coverage for MessageFlow constellation + cluster-row hardening

## Motivation

The new MessageFlow constellation (force-directed Service Bus flow) shipped with
unit tests but **no e2e coverage**. While adding it, the ui-mock and
mutation-mock suites also surfaced three scenarios that had drifted from the
current UI and were failing independently of this change.

## User-facing change

None — test-only.

## What changed

### New MessageFlow e2e coverage
- `scripts/e2e/scenarios/message-flow-events.ui.spec.ts`:
  - **enabled path**: mocks `/api/monitor/message-flow` with an active snapshot,
    asserts the card summary, expands the modal, waits for the d3 constellation
    job node, clicks it, and verifies the JSON detail modal opens. Crucially it
    asserts the **security redaction** (charter §12): the rendered job JSON must
    NOT echo the raw `owner_oid` / `tenant_id` GUIDs the detail endpoint returns
    (`redactState` strips them recursively).
  - **disabled path**: with the default `{ enabled: false }` snapshot the card
    must render nothing, so the integration-off dashboard is unchanged.
- `scripts/e2e/fixtures/mockApi.ts`: registers a default
  `/api/monitor/message-flow -> { enabled: false }` route so existing dashboard
  scenarios are unaffected (the card hides itself); the dedicated scenario
  re-registers it with an enabled snapshot.

### Pre-existing scenario drift repaired
- `scripts/e2e/pageObjects/layout.ts` `toggleTheme`: Settings is now
  section-navigated, so select the **Appearance** section before reaching the
  Theme segmented control.
- `scripts/e2e/scenarios/performance-metrics.ui.spec.ts` `openClusterDetail`:
  poll-expand the cluster row until the "Open cluster detail" button is mounted
  (a single click raced the row's collapse-state persistence) and scroll it into
  view before clicking.
- `scripts/e2e/scenarios/destructive-actions.mutation.spec.ts`: the dashboard
  polls cluster status on an interval, so the cluster row re-renders
  continuously and never satisfies Playwright's "stable" check — force the
  Stop/Delete clicks (the buttons are visible + enabled) and retry until the
  mocked action is recorded.

## Validation evidence

- `NODE_PATH=./node_modules npx playwright test --project=ui-mock
  --project=mutation-mock` (against a local vite dev server, dev-bypass auth) —
  **27 passed** (was 24 passing + 3 pre-existing failures before this change).
- New `message-flow-events.ui.spec.ts` — 2 passed (enabled + disabled paths).
- `npx eslint` on the changed e2e files — 0 errors.
