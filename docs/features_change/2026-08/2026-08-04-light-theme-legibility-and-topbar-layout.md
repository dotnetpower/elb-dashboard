---
title: Light theme legibility and top bar layout fixes
description: Apply the stored theme preference app-wide, stop the top bar from overflowing the viewport, fix the New Search grid collapse in light mode, and raise the low-contrast text found by a contrast sweep of every menu.
tags: [ui, user-guide]
---

# Light theme legibility and top bar layout fixes

## Motivation

A walk-through of every menu in light mode surfaced four classes of defect. None
of them changed what the dashboard *does*; all of them made it hard or impossible
to read.

1. **The theme preference was never applied.** `useTheme` (the only writer of the
   `data-theme` attribute on `<html>`) was mounted exclusively by
   *Settings → Appearance*. Picking "Light" worked until the next reload, and a
   user whose OS is light and whose preference is the default `system` never got
   the light palette at all unless they happened to open that panel.
2. **The top bar overflowed the viewport at every common laptop width.** The bar
   is a single `nowrap` flex row whose children do not shrink; it needed 1623 px
   of content, so at 1366 / 1440 / 1600 px the account, settings, and
   notification buttons were pushed off-screen and the *whole document* grew a
   horizontal scrollbar.
3. **The `/blast/submit` three-column grid never collapsed in light mode.**
   `[data-theme="light"] .bsl-grid` re-declared `grid-template-columns`
   unconditionally, and being more specific than the `max-width: 1100px` rule it
   overrode the collapse. Below 1100 px the stepper and summary rail were already
   `display: none`, so the centre column rendered inside the ~210 px stepper
   track — the page looked shattered.
4. **Assorted low-contrast text.** A scripted WCAG contrast sweep of Dashboard,
   New Search, BLAST Jobs, Results, Playground, Terminal, API Reference and
   Diagnostics found code blocks rendering dark ink on a dark surface, API method
   chips using dark-theme inks on light tints, and a set of `opacity` multipliers
   stacked on top of already-muted colour tokens.

## User-facing change

* The stored theme (`light` / `dark` / `system`) now applies on every screen,
  including sign-in and access-denied, and survives a reload. An inline script in
  `index.html` resolves it before first paint so there is no dark flash for a
  light-theme user.
* The top bar fits at every width from 420 px to 1920 px. Decorative chrome is
  shed widest-first as the viewport narrows: nav group labels and separators
  (≤1680), the build stamp (≤1400), the "Live" pill (≤1320), the latest-job chip
  (≤1180). The hamburger drawer now takes over at ≤940 px instead of ≤720 px —
  940 px is the narrowest viewport where the horizontal bar still fits.
* `/blast/submit` renders full-width below 1100 px in light mode, and the five
  program tabs wrap to three columns on phones instead of clipping.
* Code/sample panels (Service Bus Playground, Message Flow modal) use a light
  code canvas in light mode instead of dark text on a dark box.
* API Reference method chips (`GET` / `POST` / `DELETE` / `PUT` / `PATCH`), the
  Core section accent, and the Result Passport parity verdict resolve through
  theme tokens, so light mode gets its darker AA-legible inks. Dark mode is
  byte-identical for the method chips (the tokens hold exactly the previous
  literals).
* Disabled / dimmed controls (blocked BLAST programs, unavailable databases,
  unavailable sharding options, disabled buttons, the "last updated" whisper,
  locked terminal cockpit levels, "Coming soon" diagnostics) are still visibly
  dimmed but readable.

No route, API call, feature gate, or behaviour changed.

## Diff summary

| File | Change |
| --- | --- |
| `web/index.html` | Pre-paint `data-theme` resolution script |
| `web/src/App.tsx` | Mount `useTheme()` at the root, above every auth branch |
| `web/src/components/Layout.css` | Shedding tiers (1680 / 1400 / 1320 / 1180), drawer 720 → 940, drop the 0.6 opacity on the build stamp and nav group labels |
| `web/src/components/Layout.tsx` | `isMobileNav` 720 → 940 to match the CSS |
| `web/src/components/LatestJobChip.css` | Chip shrink tier 1320 → 1680 |
| `web/src/theme/blast-submit-layout.css` | Light grid track override moved inside `min-width: 1101px`; program tabs wrap to 3 columns ≤768 px |
| `web/src/theme/glass.css` | New `--bg-code` token (dark `#0d1117`, light `#f6f8fa`); disabled/dimmed opacity raises |
| `web/src/pages/apiReference/{constants.ts,CoreApiSection.tsx,panelStates.tsx}` | Theme tokens instead of dark-theme literals |
| `web/src/pages/blastResults/analytics/ResultPassportCard.tsx` | Parity verdict colours via tokens |
| `web/src/pages/ServiceBusPlayground.tsx`, `components/cards/MessageFlow/MessageFlowModal.tsx` | `var(--bg-code)` + hairline border on code panels |
| `web/src/pages/blastSubmit/ComputeSection.tsx`, `components/cards/ClusterPulse/atoms.tsx`, `pages/diagnostics/DiagnosticsPage.tsx` | Inline disabled `opacity` raised off the muted token |

No backend, IaC, sidecar, or image change — nothing to redeploy beyond the normal
frontend build.

## Validation

* `cd web && npm run build` — clean.
* `cd web && npx eslint src` — clean.
* `cd web && npm test -- --run` — 108 files, 978 tests passing.
* Scripted WCAG 2.1 contrast + horizontal-overflow audit run in the browser
  against the live deployment with the candidate CSS injected, on `/`,
  `/blast/submit`, `/blast/jobs`, `/blast/jobs/{id}`, `/blast/playground`,
  `/terminal`, `/docs`, `/diagnostics`:
  * Top bar width sweep at 420 / 600 / 700 / 800 / 900 / 945 / 960 / 1024 / 1100 /
    1180 / 1200 / 1280 / 1320 / 1330 / 1366 / 1400 / 1440 / 1500 / 1600 / 1680 /
    1700 / 1920 px — `document.scrollWidth === clientWidth` at every step
    (previously overflowed by up to 193 px).
  * `/blast/submit` at 1024 px: centre column measured 985 px (was 210 px).
  * Playground sample-code `<pre>`: `rgb(246,248,250)` background with
    `rgb(36,41,47)` text (was `rgb(13,17,23)` on `rgb(36,41,47)`, 1.29:1).
  * Remaining sub-4.5:1 hits are all `:disabled` controls, which WCAG 1.4.3
    exempts; they now sit at 3.0–3.5:1 instead of 1.7–2.1:1.
