---
title: Align light-theme dashboard with Azure portal / Fluent surface conventions
description: The light-theme dashboard now follows Azure portal (Fluent 2) surface conventions — neutral white card surfaces with hairline borders, colour reserved for status and small type markers, a neutral top bar with a faint blue hairline, the AKS cluster row flattened into its panel body, and clickable cluster names rendered as accent-blue links.
tags:
  - ui
---

# Align light-theme dashboard with Azure portal / Fluent surface conventions

## Motivation

The light theme had drifted toward decorative colour: each card header carried a
tinted frost band / coloured underline, the AKS cluster row was boxed inside its
panel as a "box-in-a-box", and the top bar leaned blue. Azure portal (Fluent 2)
keeps surfaces neutral and reserves colour for **status, actions, and small type
markers**, so the dashboard was realigned to that convention for a calmer, more
familiar light surface.

## User-facing change

All changes are scoped to `[data-theme="light"]` except the cluster-name link
colour, which is theme-shared (Azure renders clickable resource names blue in
both themes).

- **Menu bar** — toned from a blue tint to a neutral `#f3f3f3` surface with a
  faint blue hairline underline (Azure-portal titleBar tone). The active nav
  pill stays blue, with a slightly deeper wash + stronger ring so it still reads
  against the tinted bar.
- **Card headers** — removed the coloured frost band / underline. The surface is
  now neutral white + hairline; card type is signalled by an 8px coloured dot
  before the title plus a coloured bold title (cluster = blue, storage = green,
  acr = purple, terminal = teal, jobs = amber).
- **AKS cluster row (ClusterPulse)** — flattened into the panel body. Two
  duplicate `.cluster-pulse-card` frost-gradient definitions that were fighting
  in the cascade were removed; the box border / background / negative margin were
  dropped so the row reads as panel content aligned with the other cards' inset.
- **Cluster name** — rendered as an Azure-style clickable link
  (`color: var(--accent)`) with an underline on row hover.

No behaviour change — styling only.

## API / IaC diff summary

Frontend only:

- `web/src/theme/glass.css`
- `web/src/components/cards/ClusterPulse/PulseRowSummary.tsx`

No backend, no API, no IaC change.

## Validation evidence

- `cd web && npm run build` — type + bundle clean (`✓ built in 3.97s`).
- Dark-theme impact reviewed: the only theme-shared change is the cluster-name
  `color: var(--accent)`; `--accent` resolves to `#6e9fff` (dark, readable light
  blue on the navy canvas) and `#005fb8` (light, AA on white), so the link reads
  correctly in both themes. Every other rule is `[data-theme="light"]`-scoped, so
  dark surfaces are unaffected.
- Verified live in light theme on the local dev server: menu bar, card headers +
  dots, and cluster-row flattening. The cluster-name blue link + hover underline
  are code-applied and build-verified; live confirmation with a populated cluster
  list is pending a fresh backend Azure token.
