---
title: A11y + taxonomy UX batch (toast live-region, expand/collapse-all tree)
description: Toast notifications now announce to screen readers and have a dismiss label; the taxonomy lineage tree gains expand-all / collapse-all controls.
tags:
  - ui
  - blast
---

# A11y + taxonomy UX batch

## Motivation

Continuation of the UI/UX pass. Two more gaps from the deferred set are now
code-verifiable without a live browser: toasts were not announced to assistive
tech, and the taxonomy lineage tree could only be expanded one node at a time.

## User-facing change

- **#46 Toast live-region** — the toast container is a labelled `region`, each
  toast carries `role="alert"`/`aria-live="assertive"` for errors and
  `role="status"`/`aria-live="polite"` otherwise, so screen readers announce
  notifications. The dismiss (✕) button gains an `aria-label`.
- **#26 Taxonomy expand/collapse-all** — the Lineage tree shows **Expand all** /
  **Collapse all** buttons. A bumped force signal is broadcast to every node;
  nodes react via `useEffect` and remain individually toggleable afterwards.

## Code change summary

- [web/src/components/Toast.tsx](../../../web/src/components/Toast.tsx):
  region + per-toast `role`/`aria-live`, dismiss `aria-label`.
- [web/src/pages/blastResults/analytics/TaxonomyPanel.tsx](../../../web/src/pages/blastResults/analytics/TaxonomyPanel.tsx):
  `forceSignal` state in `LineageTree`, Expand/Collapse-all buttons, and a
  `useEffect` in `LineageNodeView` that applies the broadcast command.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run TaxonomyPanel` → 5 passed.
- `npx eslint` on both changed files → clean.

No backend / API / IaC changes.
