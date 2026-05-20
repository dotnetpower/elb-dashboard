# Light theme vividness pass

## Motivation
The light theme felt washed out: glass cards (`rgba(255,255,255,0.68)`)
blended into the aurora-gradient canvas, borders at
`rgba(80,70,160,0.14)` were nearly invisible against white, and
`--text-faint: #6c759a` did not survive the lavender wash. The user
reported "영 눈에 안들어와" (hard to read).

## User-facing change
Light mode (`data-theme="light"`) only. Dark mode untouched.

- Card surfaces lifted from 68% → 88% white (and "strong" from 85% → 96%);
  glass blur reduced 22px → 16px so content reads as a solid panel
  rather than a frosted haze.
- Card borders deepened to `rgba(60, 52, 130, 0.22)` so every panel
  has a visible edge against the canvas.
- Text scale darkened: primary `#161a36` → `#0e1230`, muted
  `#444b76` → `#353c66`, faint `#6c759a` → `#555d85`. Accent shifted
  from `#5b6cff` → `#4b5cf5` for AA contrast on white.
- Aurora canvas wash toned down (0.28 / 0.25 / 0.22 → 0.16 / 0.14 /
  0.12) and base gradient deepened (`#fbfcff → #f5f7fc` top,
  `#f0f3fb → #e8ecf6` bottom) so the page reads as
  "white card on tinted ambient" instead of "white on white".
- Per-accent panel header bands strengthened: the color-mix tint
  ramped from 12%/4% → 22%/10%, and the bottom border from 22% → 38%.
  Cluster (blue), Storage (emerald), ACR (purple), Terminal (teal),
  and Jobs (amber) headers now carry an unambiguous identity stripe.
- Drop shadow rebalanced: short 1px-3px sharpen layer added so cards
  feel anchored without raising the elevation budget above the
  charter's 32px cap.

## API / IaC diff summary
None. CSS-only.

## Files touched
- [web/src/theme/glass.css](../../../web/src/theme/glass.css) —
  light-theme variable block + aurora canvas + per-accent panel-hd
  gradients.

## Validation
- Manual visual check on `http://127.0.0.1:8090/` and
  `/blast/jobs/<id>?tab=run` in light mode (Playwright screenshot
  captured before / after).
- Dark mode rendered on the same URL after switching `elb-theme` back
  to `dark` — no change confirmed by screenshot.
- All edits live under `[data-theme="light"]` selectors so the dark
  cascade is provably untouched.
