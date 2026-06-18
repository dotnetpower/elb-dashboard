---
title: Modal accessibility pass — shared focus-trap primitive + ARIA/scroll-lock fixes
description: Hardened the shared useFocusTrap hook (nest-safe body scroll lock, return-focus, optional Escape) so every modal gains it, and filled in missing dialog roles, accessible names, focus traps and close-button labels across the hand-rolled modals.
tags:
  - ui
---

# Modal accessibility pass

## Motivation

The frontend has ~22 hand-rolled modal / dialog / drawer / overlay components and
**no shared modal primitive**, so accessibility was inconsistent: some had a focus
trap, some an Escape handler, some neither; none locked background scroll except
two; and several were missing `role="dialog"` / `aria-modal` / an accessible name.
A keyboard or screen-reader user got a different (often broken) experience per
modal.

## User-facing change

### Shared primitive: `useFocusTrap` hardened (one place, every modal benefits)

[web/src/hooks/useFocusTrap.ts](../../../web/src/hooks/useFocusTrap.ts) kept its
Tab-cycling + initial-focus behaviour and gained, for **every** caller:

- **Body scroll lock** while open, nest-safe via ref counting (stacked/nested
  modals don't release the lock early), with scrollbar-width compensation so the
  page doesn't shift when the scrollbar disappears.
- **Return focus** to the triggering element on close (WCAG 2.4.3), guarded so it
  never steals focus the app moved elsewhere.
- **Optional Escape-to-close** via a new optional `onClose` arg (modals that
  already had their own Escape pass nothing and keep it).
- Disabled controls excluded from the focus cycle.

This instantly upgraded the ~12 modals already using the hook (TaxonomyModal,
TaxonomyDetailModal, SettingsPanel, ConfirmDialog, ProvisionModal,
BlastDbClusterConfirm, …) with scroll-lock + return-focus they previously lacked.

### Per-modal fixes (dialog semantics + adopting the hook)

| Modal | Added |
| --- | --- |
| SetupWizard | `role`, `aria-modal`, `aria-labelledby` (+title id), focus trap, Escape, scroll lock, return focus, close-button `aria-label`, `type="button"` |
| GettingStartedGuide | same set as SetupWizard |
| KeyboardShortcuts / Help overlay | `role`, `aria-modal`, `aria-labelledby` (+title id), focus trap, scroll lock, return focus |
| SidecarLogModal | focus trap, scroll lock, return focus (already had role/aria + Escape) |
| PodLogsDialog | focus trap, Escape, scroll lock, return focus, close-button `aria-label` |
| PodDescribeDialog | focus trap, Escape, scroll lock, return focus, close-button `aria-label` |
| MessageFlowModal | Tab focus trap + return focus; manual body lock replaced by the nest-safe ref-counted lock (kept its nested-modal-aware Escape) |

## API / IaC diff summary

None. Frontend-only (`web/src/`); no API, schema, or infra changes.

## Validation evidence

- Browser-verified the hardened hook on the live taxonomy modal: on open
  `body.overflow="hidden"` + `padding-right:10px` (scrollbar compensation) and
  focus moved into the dialog; on Escape the modal closed, `overflow`/`padding`
  restored, and **focus returned to the "Choose taxon" trigger**.
- `cd web && npx tsc --noEmit` → 0 errors (the hook's new optional arg is
  backward compatible with all existing callers).
- `cd web && npm run build` → `✓ built in 5.00s`.
- `npx eslint` on all touched modal files → 0 problems.
