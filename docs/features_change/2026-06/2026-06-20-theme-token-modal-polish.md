---
title: App-wide theme-token and modal polish pass (30+ fixes)
description: A systematic audit fixing undefined design tokens, hardcoded light-breaking colors, monospace literals, an invisible light-mode badge, and a missing modal Escape handler across many screens.
tags:
  - ui
  - architecture
---

# App-wide theme-token & modal polish pass

## Motivation

Following the sequence-builder modal overhaul, an app-wide audit (all screens +
modals, both the dark Grafana palette and the light VS Code palette) surfaced a
class of systemic issues: several **CSS custom properties were referenced but
never defined**, so they silently fell back to `inherit` or to dark-only hex
fallbacks that washed out in light mode; many inline styles used **hardcoded
`rgba(255,255,255,…)` surfaces** that are invisible on a white panel; a few used
**bare `monospace` font literals** that bypass the unified `--font-mono` token;
one badge was **white-on-white in light mode**; and the FASTA-templates modal had
**no Escape-to-close**.

## User-facing change (30+ discrete fixes)

**New / fixed design tokens (`glass.css`) — these alone correct ~80 render sites:**

- `--text-secondary` — was undefined (≈25 call sites fell back to `inherit`,
  flattening the text hierarchy and breaking active/inactive tab contrast). Now a
  real 3rd level: `#c2c8d2` dark / `#3b3b3b` light.
- `--text` — was undefined (~20 call sites relied on `inherit` by accident). Now
  aliased to `--text-primary`.
- `--text-code` — was undefined; the `#c9d1d9` fallback was illegible on the
  light code inset. Now `#c9d1d9` dark / `#24292f` light.
- `--text-warning` → alias of `--warning` (the old `#d29922` fallback failed AA
  on white).
- `--surface-2` → alias of `--bg-tertiary` (the old `rgba(255,255,255,0.06)`
  fallback was invisible in light mode).
- `--json-key/str/num/bool/nil/brace` — new theme-aware JSON syntax-highlight
  tokens (dark inks + AA-legible light overrides); the API-reference JSON viewer
  was previously dark-only pastels, illegible on the light inset.
- New `.glass-badge--accent` modifier (accent tint + border + text) for badges
  that must read on a white panel.

**Per-component fixes (all hardcoded → theme tokens):**

- `JobLine` (×2) and `ConfirmDialog` — bare `monospace` literals → `--font-mono`;
  `ConfirmDialog` confirm input surface `rgba`/`--surface-2` fallback →
  `--bg-tertiary`.
- `QuerySection` — FASTA-templates badge → `glass-badge--accent` (was invisible
  in light); **added Escape-to-close** to the FASTA-templates dialog.
- `JsonHighlight` — 6 hardcoded hex colors → the new `--json-*` tokens.
- `ResponseViewer` (×2), `BlastResults` warning banner, `EndpointResponsesDoc`
  (×2), `ComputeSection` skeletons (×2), `K8sJobsPanel` zebra stripe,
  `ClusterHeaderBand`, `AutoStopPanel` (×2), `StepRow` skipped chip,
  `WarmupSection` (×5: row, skeleton, notice, progress track, chip),
  `SequenceBlocks` warning underline (`#f0a868` → `--warning`) — all
  `rgba(255,255,255,…)` / dark-only hex → theme-aware tokens / `color-mix`.

`SequenceBlocks`' base-grid monospace was intentionally **left** (it needs a true
fixed-width font for column alignment, which the Inter-unified `--font-mono`
would break). The pod-logs / terminal consoles were left dark-on-purpose.

## API / IaC diff summary

Frontend only: `web/src/theme/glass.css` (token + class additions) plus 14
component/page files swapping hardcoded values for tokens. No backend, API, or
IaC changes; no business logic touched.

## Validation evidence

- `npm run build` — succeeds; `npx eslint` on all 15 touched files — clean.
- `npx vitest run src/pages/blastSubmit` — 217 passed (incl. SequenceBuilderDialog).
- Live host-mode check at `http://localhost:8090`: confirmed the FASTA-templates
  badge is now visible in **both dark and light** themes, the modal renders
  correctly in light, and the newly-added Escape-to-close works. Theme restored
  to System afterwards.
