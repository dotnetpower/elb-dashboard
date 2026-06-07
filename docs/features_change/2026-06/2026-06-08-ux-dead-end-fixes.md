---
title: Fix three UX dead-ends — empty escape-hatch copy, orphaned storage remediate text, silent API-Reference copy
description: Removes affordances that signal an action but deliver nothing — an empty escape-hatch command block with a no-op copy button, a storage warning that says "remediate" with no next step, and an API-Reference copy-link button that gave no feedback when the clipboard was unavailable.
tags:
  - ui
  - user-guide
---

# Three UX dead-end fixes (2026-06-08)

A follow-up to the update-indicator deep-link fix, hunting the same bug class:
"the affordance is shown, but acting on it leads nowhere." Three confirmed
dead-ends fixed; the rest of the audit candidates were verified as already
guarded (disabled states, conditional rendering) and left unchanged.

## 1. Empty escape-hatch command block (Upgrade page)

`web/src/pages/UpgradePage.tsx` rendered the "Escape-hatch commands" section
whenever `escape` was non-null (`{escape && (...)}`). If the backend returned an
escape-hatch with an **empty** `commands` array, the user saw an empty `<pre>`
and a "Copy commands" button that wrote an empty string to the clipboard — paste
into a shell, nothing happens.

Fix: render the section only when `escape.commands.length > 0`, and guard the
copy handler (`if (!text || !navigator.clipboard) return;`).

## 2. Orphaned "remediate" text on the storage public-access warning

`web/src/components/cards/storage/StorageWarnings.tsx` showed
`"Expected: Private only · Investigate and remediate"` on the incident-grade
public-access banner, but there was no button or link to investigate or
remediate — the word "remediate" promised an action the UI did not offer. The
banner is intentionally non-dismissible (security), so the right fix is honest,
actionable text rather than a button.

Fix: the detail now states the concrete next step — set the account to Private
only in the Azure Portal, or run `scripts/dev/storage-public-access.sh off` for
a local-debug session — instead of an empty "remediate" promise.

## 3. Silent API-Reference "copy link" button

`web/src/pages/apiReference/EndpointCard.tsx` flashed the "Link copied"
confirmation only inside the clipboard `.then(onSuccess)` callback. When
`navigator.clipboard` was unavailable (insecure context) or the write was
denied, no callback fired and the button gave **no feedback at all**, even
though the deep-link had already been written to the address bar.

Fix: update the address bar (the real fallback share mechanism) and flash the
confirmation immediately, then attempt the clipboard write best-effort. The user
now always gets feedback and always has the shareable URL.

## API / IaC diff summary

- `web/src/pages/UpgradePage.tsx` — escape-hatch section gated on
  `escape.commands.length > 0`; copy handler guards empty text / missing
  clipboard.
- `web/src/components/cards/storage/StorageWarnings.tsx` — public-access banner
  detail rewritten to a concrete remediation step.
- `web/src/pages/apiReference/EndpointCard.tsx` — `handleCopyLink` flashes
  confirmation after the address-bar update regardless of clipboard outcome.
- No backend / IaC change.

## Validation evidence

- `cd web && npx vitest run` — 729 passed (78 files), no regression.
- `cd web && npx tsc --noEmit` and `npx eslint` on the three files — clean.
- `cd web && npm run build` — clean.

## Audit candidates verified as NOT bugs (left unchanged)

- Breadcrumb navigation (whitelist-guarded), LatestJobChip links, the `/upgrade`
  link, Diagnostics "Coming soon" (properly `disabled`), Public HTTPS copy
  (rendered only when a URL exists), BuildLogViewer copy (`disabled={!content}`),
  cluster detail modal trigger — all confirmed correctly wired. The
  database-chip "view-only vs clickable" visual distinction is a minor styling
  nit, not a dead-end, and was not changed.
