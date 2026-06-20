---
title: Sequence builder ("Generate query") modal UI/UX overhaul
description: A 20-point UI/UX pass on the New Search "Generate query from NCBI" modal, reviewed in both dark and light themes.
tags:
  - ui
  - blast
---

# Sequence builder ("Generate query") modal UI/UX overhaul

## Motivation

The New Search → **Generate query from NCBI** modal
([SequenceBuilderDialog](../../../web/src/pages/blastSubmit/SequenceBuilderDialog.tsx))
was built almost entirely from ad-hoc inline styles. It had no visible step
structure, weak hover/selected states, a semantically wrong (green "example")
primary button, a literal `monospace` font that bypassed the design system, no
viewport-overflow handling, and several accessibility gaps. The colours and
contrast also needed a review in both the dark Grafana palette and the light VS
Code palette.

## User-facing change

A 20-point pass, all driven by design tokens so the modal tracks both themes
without per-theme overrides:

1. Dedicated `.seqbuilder-*` CSS classes replace the inline-style soup.
2. Fixed-height shell with an internally scrolling body — the modal never
   overflows the viewport even with a long result/feature list.
3. Pinned header (badge + title + subtitle + close) stays visible while scrolling.
4. Pinned footer action bar — Cancel / Insert always reachable.
5. The three NCBI steps are now **visible numbered markers** ("1 Find a record",
   "2 Accession & genes", "3 Sub-range & strand"), not code comments.
6. "Insert sequence" uses a proper **primary** button (was the green "example"
   class — a semantic mismatch).
7. Strand toggle is now a real **segmented control** with a clear selected state
   in both themes.
8. **Enter** on the accession field loads genes (previously only the search
   field handled Enter).
9. Literal `monospace` replaced with the `--font-mono` design token across the
   accession field, result accession, feature name, and FASTA preview.
10. Result cards get real hover + a **selected indicator** (check icon + accent
    ring) instead of an inline background only.
11. Feature rows get hover + consistent padding via the shared item class.
12. **Loading placeholders** ("Searching NCBI…", "Loading gene features…") in the
    list region, not just a spinner in the button.
13. The result region only renders once a search runs (no empty box flashing).
14. A **"<accession> selected"** confirmation chip + a `visible/total` feature
    count caption next to the filter.
15. **Escape closes** the modal and the search field **autofocuses** on open.
16. Every input has an explicit **aria-label** (was placeholder-only).
17. The FASTA preview is an **`aria-live`** region so screen readers announce the
    resolved header.
18. The NCBI badge carries an accent tint so it is visible on light's white panel
    (the old `glass-badge` was white-on-white in light mode).
19. The preview block uses `--text-primary` on a `--code-surface` inset for
    adequate contrast (placeholder stays faint + italic).
20. Step dividers + spacing-token rhythm replace the mixed 6/10/12 px inline
    margins.

## API / IaC diff summary

- `web/src/pages/blastSubmit/SequenceBuilderDialog.tsx` — full JSX restructure
  (header/body/footer shell, numbered steps, segmented strand, selected/loading
  states), Escape + autofocus effect, aria labels. The exported pure helpers
  (`buildSubrange`, `previewFastaHeader`, `errorMessage`) and the component prop
  contract (`onClose`, `onInsert`, `toast`) are unchanged.
- `web/src/theme/glass.css` — new token-only `.seqbuilder-*` block.

No backend, API, or IaC changes.

## Validation evidence

- `npx eslint src/pages/blastSubmit/SequenceBuilderDialog.tsx` — clean.
- `npx vitest run src/pages/blastSubmit/SequenceBuilderDialog.test.ts` — 14 passed
  (the pure-helper contract is untouched).
- `npm run build` — built successfully.
- Live host-mode verification at `http://localhost:8090/blast/submit`: opened the
  modal, ran a real NCBI search (`monkeypox virus complete genome`), selected a
  record, and confirmed the selected/preview/footer states in **both dark and
  light themes**. Escape-to-close and search autofocus confirmed.
