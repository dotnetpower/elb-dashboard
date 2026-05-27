# Progressive nav collapse — Tools group → "More ▾" dropdown before hamburger

## Motivation
The top navigation collapsed to the hamburger drawer at a single
breakpoint (1320 px), meaning the entire nav disappeared in one step
when a user resized the window from a wide desktop to a 13" laptop /
external monitor at 1280 px. The Tools group (Lab Tools, Terminal,
API) is the lowest-priority cluster — primary entry points
(Dashboard, New Search, Recent searches) deserve to stay visible
longer.

## User-facing change
- **Tier A — ≥1320 px** (unchanged): full horizontal nav with Monitor /
  BLAST / Tools groups inline.
- **Tier B — 720–1320 px** (new): the Tools group (Lab Tools,
  Terminal, API) collapses into a `More ▾` dropdown anchored at the
  right of the nav. Monitor + BLAST stay first-class horizontally. The
  trigger picks up the `.active` highlight + a small dot when the
  current route lives inside the dropdown (e.g. you are on `/docs` and
  the `More` chip glows so you still know "you are here").
- **Tier C — <720 px** (unchanged behaviour, moved breakpoint): the
  existing hamburger drawer takes over. Every nav item is listed
  vertically as before.

The dropdown closes on outside click, on `Escape`, and when the user
selects any item inside (so the page transition isn't blocked by an
open popover).

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Hook | [web/src/hooks/useMediaQuery.ts](../../../web/src/hooks/useMediaQuery.ts) (new) | Shared SSR-safe `useMediaQuery(query)` hook. Two existing call sites (`useGettingStartedReadiness`, `visibilityHooks`) keep their inline copies for now — they can migrate in a follow-up. |
| Component | [web/src/components/NavMoreDropdown.tsx](../../../web/src/components/NavMoreDropdown.tsx) (new) | Glass dropdown for nav overflow. Click-outside + Escape close. MutationObserver on its children watches React Router's `.active` class so the trigger reflects "you are on a route inside this menu". |
| Layout | [web/src/components/Layout.tsx](../../../web/src/components/Layout.tsx) | Added `useMediaQuery` calls → `isCompactNav` / `isMobileNav` / `useToolsDropdown` flags. Tools group is rendered either inline (Tier A) or inside `NavMoreDropdown` (Tier B). Tier C continues to render the inline variant (the drawer stacks it vertically — no nested dropdown). |
| Styles | [web/src/components/Layout.css](../../../web/src/components/Layout.css) | Hamburger media-query breakpoint moved from `max-width: 1320px` to `max-width: 720px`. New `.layout__nav-more`, `.layout__nav-more-trigger`, `.layout__nav-more-panel`, `.layout__nav-more-dot` rules. |

No backend change. No new dependency.

## Validation evidence
- `cd web && npx tsc -p tsconfig.json --noEmit` on the changed files → clean.
- `cd web && npx eslint src/components/Layout.tsx src/components/NavMoreDropdown.tsx src/hooks/useMediaQuery.ts` → clean.
- `cd web && npm run build` → ✓ built in 9.71s (the existing chunk-size warning is unrelated).
- DevTools responsive mode walkthrough at 1920 → 1320 → 1100 → 800 → 600 px:
  - 1920–1320 px: full nav (Tier A).
  - 1320 → 720 px: Tools group disappears from inline, `More ▾` appears on the right. Clicking it opens a panel with Lab Tools (when enabled) / Terminal (when enabled) / API. Navigating to API → `More` chip highlights with a dot. ESC / outside click closes the panel.
  - Below 720 px: hamburger appears, nav becomes the vertical drawer with every item (drawer renders the inline branch, not the dropdown).

## Follow-ups (not in this PR)
- The two existing inline `useMediaQuery`-style hooks
  (`useGettingStartedReadiness`, `SidecarsCard/visibilityHooks`) can
  migrate to the new shared hook in a tidy-up commit — left alone here
  to keep the diff focused on the responsive nav behaviour.
- If "API" turns out to be heavily used at Tier B, lifting it out of
  the dropdown (and only collapsing Lab Tools + Terminal) is a one-line
  JSX change.
