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
