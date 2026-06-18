---
title: Light theme title-bar chrome tone (VS Code Light Modern)
description: Light theme app bar adopts the VS Code Light Modern titleBar tone (#f8f8f8) so the chrome reads above the white content panels; the already-correct primary-button hover and control-border values were re-aligned to HEAD.
tags:
  - ui
---

# Light theme — VS Code Light Modern title-bar chrome

## Motivation

The light palette is already a faithful "Light Modern" implementation
(`#f8f8f8` canvas, `#ffffff` panels, `#e5e5e5` hairlines, `#005fb8` accent). One
remaining divergence from the real VS Code Light Modern theme was the top app
bar: it was pure `#ffffff`, while VS Code uses `titleBar.activeBackground`
`#f8f8f8` so the chrome reads as a band above the white content panels. Every
rule stays scoped to `[data-theme="light"]` — dark mode is unaffected.

## User-facing change

- **Top app bar tone** `#ffffff` → `#f8f8f8` (VS Code `titleBar.activeBackground`)
  across all three light `.layout__topbar` blocks, retaining the hairline base
  border so the chrome still separates from the canvas. This is the only change
  versus `HEAD`.

Verified-and-aligned (these already matched VS Code Light Modern in `HEAD`; the
working tree had drifted to older values, which this pass restored — so they
carry no net diff versus `HEAD`):

- Primary button hover darkens (`#005fb8` → `#0258a8`, VS Code
  `button.hoverBackground`) rather than lightening (`#0a6cc9`).
- Control border token `--border-medium` is `#cecece` (VS Code `input.border`
  / `dropdown.border`), not `#d4d4d4`.
- Disabled primary button keeps the flat disabled fill on hover instead of a
  taupe `rgba(69, 65, 66, …)` reaction.

Deliberately left unchanged after analysis:

- The warm-looking `rgba(69, 65, 66, …)` neutrals were kept — composited over
  white at their 0.06–0.45 alphas they are effectively neutral grey (e.g. 0.18 →
  `rgb(222, 221, 221)`), so neutralising ~50 sites would be high churn for an
  imperceptible change.
- Body text stays `#1f1f1f` (crisper for dense dashboard data; also a legitimate
  VS Code activity-bar / list-active foreground) rather than dropping to
  `#3b3b3b`.
- The active-nav accent pill (`#e8f1fb`) is kept over VS Code's flat neutral
  selection — it reads better and still carries the accent identity.

## API / IaC diff summary

None. CSS-only change in [web/src/theme/glass.css](../../../web/src/theme/glass.css);
no API, schema, or infra changes.

## Validation evidence

- `cd web && npm run build` → `✓ built in 3.84s` (clean).
- `git diff web/src/theme/glass.css` is the three `.layout__topbar` blocks only
  (`#ffffff` → `#f8f8f8`); `git status --short` shows only `glass.css` + this note.
- Vite HMR applied the edits live with no errors.
- Light theme rendered at `http://localhost:8090/` with `data-theme="light"`
  forced; verified `topbar` computed background is `rgb(248, 248, 248)` and body
  canvas `rgb(248, 248, 248)`. Dashboard chrome (white panels, blue nav pill,
  coloured card headers, flat white chips, blue primary CTA) screenshot-checked.
- `uv run python scripts/docs/check_frontmatter.py` → OK.

## Follow-up: accessibility contrast pass (WCAG AA)

A second pass walked every menu in light mode with a computed-contrast audit
(WCAG 2.1 1.4.3: ≥4.5:1 normal text, ≥3:1 large) and fixed the failures so
secondary text reads clearly. All changes stay scoped to `[data-theme="light"]`
(plus one theme-flipping token); dark mode is unaffected.

- **`--text-faint` `#8a8a8a` → `#6b6b6b`.** The faint ramp failed AA at
  ~3.25–3.45:1 and was used for ~40+ elements per page (eyebrows, captions,
  card subtitles, summary-rail keys, `optional`/`advanced` labels, version
  stamp, metadata). `#6b6b6b` is ~5.1:1 on white / ~5.0:1 on the `#f8f8f8`
  canvas and stays clearly secondary against `#1f1f1f`.
- **`--purple` / `--card-accent-acr` `#8661c5` → `#7a52bc`.** Purple eyebrow
  text sat at 4.37:1 (just under AA); the darker purple passes while keeping the
  ACR card identity.
- **`--warning` `#9a6700` → `#8a5d00`.** The amber risk chip text was 4.39:1 on
  its tinted background; the darker amber clears AA.
- **Disabled "Run BLAST" submit button** (`.blast-submit-btn:disabled`) was
  white-on-`#b1b3b3` (2.1:1, read as broken rather than disabled) → the sibling
  preflight button's `#f2f2f2` / `#d7d7d7` / `#6b6f73` disabled style.
- **DatabaseBuilder step badge** used hardcoded dark-navy `#0b132b` ink that only
  works on dark theme's bright fills; in light mode the fills are saturated
  (accent/success/grey) so the number/check fell to 2.9:1. Added a theme-flipping
  `--step-badge-ink` token (dark `#0b132b`, light `#ffffff`) — dark theme is
  byte-identical, light now passes (white on accent 6.3:1, success 5.2:1,
  grey 5.3:1).
- **Live Wall log console** (`.live-tile__log`, a fixed-dark `#0a0c10` surface in
  both themes) inherited theme text tokens, so light mode flipped log messages to
  dark-on-dark (`--text-primary` `#1f1f1f`, near-invisible) and the empty state to
  3.67:1. Pinned the log block's text to the dark-surface palette under light.

### Validation evidence (contrast pass)

- Per-page computed-contrast audit re-run after the fixes returns **zero AA
  failures** on Dashboard, New Search, Recent searches, Terminal, API, Playground,
  Diagnostics, Upgrade, Live Wall, Custom DB, and the Settings panel
  (Appearance + Telemetry).
- `cd web && npm run build` → `✓ built in 3.81s` (clean).
- Most of these edits landed in `HEAD` (the maintainer's mid-session commits
  swept up the dirty theme files); the remaining uncommitted delta is
  [web/src/pages/Monitor/LiveWall.css](../../../web/src/pages/Monitor/LiveWall.css)
  plus this note.

### Deeper sweep — overlays, placeholders, focus, disabled labels

A follow-up sweep covered the surfaces the first pass did not, using an
alpha-composited contrast calculation (tinted/translucent backgrounds composited
to their true effective colour rather than the first opaque ancestor):

- **Disabled primary button label** (`.glass-button--primary:disabled`) was
  `rgba(31,31,31,0.38)` ≈ 2.26:1 (barely legible, e.g. the taxonomy modal
  `Apply`). Disabled controls are WCAG-exempt, but for readability and to match
  the already-fixed disabled submit button it is now `#6e6e6e` (~4.3:1 on
  `#ebebeb`) — clearly legible yet still muted/off.
- **Placeholders** verified: input/textarea placeholders are `#757575`/`#6b6b6b`
  at 4.6–5.3:1 (pass).
- **Keyboard focus** verified by a real Tab walk: 21/22 focusable elements show a
  visible 3px outline (nav `#525252` ~7:1 on the title-bar, active item the accent
  blue); the one select without its own outline is covered by its parent chip's
  `:focus-within` ring.
- **Overlays** audited and clean: taxonomy filter modal, keyboard-shortcuts
  overlay, and the user menu (only the WCAG-exempt disabled button surfaced, now
  fixed above).

### Validation evidence (deeper sweep)

- Alpha-composited audit returns **zero AA failures** on Dashboard and New Search
  (incl. the open taxonomy modal); overlays and menus clean.
- `cd web && npm run build` → `✓ built in 4.27s` (clean).

### Third sweep — non-text contrast & data-only status badges

A non-text (WCAG 1.4.11) and data-state sweep covered surfaces that only appear
with live data (status badges) by rendering each badge class and measuring its
composited contrast:

- **Success status badge** (`gt-g` / `dv3-pill-success`, i.e. Ready / Complete /
  Succeeded) was the green `--success` `#1a7f37` on its green-tinted pill — only
  **4.44:1**, just under AA. It is hidden in the empty/error states the earlier
  passes saw, but appears constantly on real data. Darkened `--success` (and the
  matching `--card-accent-storage`) to `#15732e` → **5.21:1**, still a clear
  forest green. Dark theme (`#73bf69`) is untouched.
- Re-measured all badge systems after the fix: `gt-*` and `dv3-pill-*` now span
  4.8–7.1:1 (all pass); the live `Live` / `Loading` / `Error` card badges are
  6.34 / 4.93 / 5.49:1.
- **Icons** (lucide, `stroke=currentColor`) inherit the now-AA text colours, so
  meaningful icons clear the 3:1 non-text bar; control borders keep the VS Code
  Light Modern hairline (`#cecece`/`#e5e5e5`) by design.
- **Use of colour (1.4.1)**: status is always paired with a text label
  (`Error`, `Loading`, `Ready`…), not colour alone.

### Validation evidence (third sweep)

- Badge-injection contrast test: success 4.44 → 5.21; all `gt-*` / `dv3-pill-*`
  ≥ 4.8:1.
- Full-page composited audit re-run: **zero AA failures** on Dashboard and New
  Search after the green change (no regression).
- `cd web && npm run build` → `✓ built in 4.13s` (clean).
- Settings panel: Appearance + Telemetry sections were audited clean earlier; the
  remaining sections share the same fixed tokens/components (panel re-open was
  flaky against the no-backend dev server, so they were not individually
  re-walked this pass).

### Observed (not fixed) — theme not applied at app root

`useTheme` (which sets `data-theme` on `<html>`) is mounted only inside the
Settings → Appearance section, so on a fresh reload the attribute is absent until
that panel is opened. Worth hoisting theme application to the app root (or an
`index.html` FOUC script) so the chosen theme persists across reloads. Out of
scope for this contrast pass.
