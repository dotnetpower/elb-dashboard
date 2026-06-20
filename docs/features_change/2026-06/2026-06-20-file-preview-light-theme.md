---
title: Light-theme readability for INI/FASTA file previews
description: The Execution Steps config (elastic-blast.ini) and input.fa previews now use theme-aware colours so their keys/bases stay legible in light mode.
tags:
  - ui
  - blast
---

# Light-theme readability for INI/FASTA file previews

## Motivation

On the Recent searches job-detail **Run details → Execution Steps** view, the
`Configure` step's `elastic-blast.ini` preview was hard to read in light theme:
the config **keys** (left of `=`, e.g. `azure-region`, `machine-type`,
`num-nodes`, `pd-size`) were nearly invisible.

Root cause: `HighlightedINI` / `HighlightedFASTA` in
[web/src/components/BlastFilePreview.tsx](../../../web/src/components/BlastFilePreview.tsx)
hard-coded dark-theme values — a dark inset background (`rgba(0,0,0,0.25)`) and a
light-grey key colour (`#9aa3b8`) / bright nucleotide base colours. In light
theme the dark inset became a washed mid-grey box and the light inks dropped to
~1.4:1 contrast.

## User-facing change

* The config (`elastic-blast.ini`) and FASTA (`input.fa`) previews are now
  theme-aware: in light mode the inset surface lightens and the INI keys +
  nucleotide bases (A/T/G/C/U) darken to AA-legible colours; dark mode is
  visually unchanged.
* No layout, content, or behaviour change — colours only.

## API/IaC diff summary

* [web/src/theme/glass.css](../../../web/src/theme/glass.css) — added
  `--code-surface` + `--seq-a/-t/-g/-c/-u` tokens (dark defaults + darkened
  `[data-theme="light"]` overrides).
* [web/src/components/BlastFilePreview.tsx](../../../web/src/components/BlastFilePreview.tsx)
  — `HighlightedINI` background → `var(--code-surface)`, key colour `#9aa3b8` →
  `var(--text-muted)`; `HighlightedFASTA` background → `var(--code-surface)`,
  base colour map → the `--seq-*` tokens.

## Validation evidence

* `cd web && npm run build` → built clean (no type errors).
* Live light-theme simulation on the deployed job-detail page (overriding the
  old inline colours with the new token values) confirmed all INI keys/values
  render legibly — screenshot captured in the session.
* Scope: only the BlastFilePreview INI/FASTA previews. Other hard-coded base
  colour maps (`AlignmentViewer`, `LineageTree`) are a separate surface and were
  intentionally left unchanged.
