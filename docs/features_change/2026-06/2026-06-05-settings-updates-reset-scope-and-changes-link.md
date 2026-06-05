---
title: Settings → Updates — scoped Reset, always-visible upgrade CTA, and a "View changes" link
description: The Settings footer Reset button now only appears on the
  preference-backed sections it actually affects, the self-upgrade link is
  always reachable from Settings → Updates (and turns into an "Update now" CTA
  when an update is available), and both Settings and the Upgrade page surface a
  GitHub "compare" link so an operator can read which commits an update brings in.
tags:
  - ui
  - release
---

# Settings → Updates: scoped Reset + visible upgrade CTA + change diff link

## Motivation
Three papercuts reported from the Settings panel:

1. **Reset looked broken.** The footer `Reset` button clears the browser-local
   preferences (`localStorage["elb-prefs"]`: theme, preview flags, telemetry /
   connection string). It worked, but it was *shown on every section except
   Preview* (`showFooterActions = active !== "preview"`) — i.e. it appeared on
   Resources / Updates / AKS / … where there is nothing prefs-backed to reset
   (so it read as "the button does nothing"), and was hidden on the Preview
   section where it is most relevant.
2. **No visible "update" button.** Settings → Updates only rendered the
   "Manage upgrade → Open" link when the persisted state row already had a
   `git_remote`, and the actual start control lives on the separate `/upgrade`
   page. A new commit could be surfaced with no obvious way to act on it.
3. **No way to see what changed.** Neither surface showed which commits an
   update would bring in.

## User-facing change
- **Reset is now scoped to the sections it affects.** The footer `Reset` button
  appears only on **Appearance**, **Preview**, and **Telemetry** (the
  `usePreferences` / `useTheme`-backed sections). On the other sections it is
  hidden instead of appearing inert. Behaviour of the button itself is
  unchanged (still gated by the existing confirm dialog).
- **The self-upgrade link is always visible** in Settings → Updates. When an
  update is available (a newer release tag, or a new commit when the preview
  channel is on) the row becomes a primary **"Update now"** call-to-action with
  an up-arrow icon; otherwise it is the neutral **"Manage upgrade → Open"**
  link. Both navigate to `/upgrade`.
- **"What's new" / "View changes" link.** When an update is available and the
  remote is a GitHub repo, Settings → Updates shows a **View changes** link and
  the `/upgrade` "Start an upgrade" section shows **View changes on GitHub** —
  both open the GitHub `compare/<running>...<latest>` view so the operator can
  read the exact commit range.

## API / IaC diff summary
- No backend or IaC change. Reuses the existing `upgradeApi` typed client and
  the build-time `__APP_COMMIT__` stamp.
- Frontend only:
  - `web/src/api/upgrade.ts` — new pure helpers `githubRepoBaseUrl()` and
    `githubCompareUrl()` (normalise a GitHub HTTPS/SSH `.git` remote, strip
    credentials, and build the compare URL; return `null` for non-GitHub or an
    empty/equal range).
  - `web/src/api/upgrade.test.ts` — new unit tests for the helpers (13 cases).
  - `web/src/components/SettingsPanel.tsx` — `PREF_BACKED_SECTIONS` gate for the
    footer Reset; "What's new" row; always-visible "Update now / Manage upgrade"
    row.
  - `web/src/pages/UpgradePage.tsx` — "View changes on GitHub" link in the Start
    section.

## Validation evidence
- `npx vitest run src/api/upgrade.test.ts` — 13/13 passing.
- `npx vitest run` (full web suite) — 75 files / 695 tests passing.
- `npx eslint` on the four touched files — clean.
- `npm run build` — succeeds.
- Diff audit: only the four intended `web/` files are dirty.
