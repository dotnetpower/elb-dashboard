---
title: NCBI-style colored full-sequence view on Sequence Detail
description: Replace the cramped FASTA scroll box with an NCBI ORIGIN-style aligned, color-coded full sequence renderer.
tags:
  - ui
  - blast
---

# NCBI-style colored full-sequence view

## Motivation

On the Sequence Detail page the resolved FASTA was already shown in full, but it
rendered inside a small `maxHeight: 320` scroll box with `word-break: break-all`.
That made the sequence *look* truncated and gave no positional context — nothing
like NCBI's GenBank ORIGIN block. A researcher reported they could not tell the
full sequence was present, and asked for an NCBI-style aligned, colored view.

## User-facing change

- The "Sequence (FASTA preview)" card is now just "Sequence" and renders the
  **entire** resolved sequence (no truncation) in NCBI ORIGIN style:
  - left 1-based position gutter (right-aligned),
  - six space-separated groups of ten residues per row (60/row),
  - per-nucleotide color (A=green, C=blue, G=amber, T/U=red; N/other muted),
  - soft-masked lowercase residues dimmed,
  - the requested hit range (`hl_start`/`hl_stop`) highlighted with a background.
- A small color legend and the residue count are shown above the block.
- The vertical viewport grew from 320 px to `70vh` so far more of the sequence
  is visible at once; the `Copy FASTA` button and hit-range badge are unchanged.

### Performance handling (no truncation)

Per-base coloring emits one `<span>` per residue, and the backend FASTA cap
allows up to ~5 million bases, so coloring is length-gated:

- `≤ 20,000 bp`: colored automatically, with `content-visibility: auto` rows so
  off-screen rows are not painted.
- `> 20,000 bp`: rendered as a single aligned `<pre>` (one DOM node, plain text)
  — still the **complete** sequence — with an opt-in "Colorize (slower)" button
  up to a `200,000 bp` hard cap.
- `> 200,000 bp`: coloring stays off (note shown), full aligned text still
  rendered.

Protein records (heuristically detected) skip nucleotide coloring and render the
aligned plain text.

## API / IaC diff summary

- No API or IaC change. Frontend-only.
- New component: `web/src/pages/sequence/SequenceBlocks.tsx`.
- `web/src/pages/sequence/SequenceDetail.tsx`: replaced the FASTA `<pre>` with
  `<SequenceBlocks fasta={previewFasta} highlight={highlightRange} />`.

## Validation evidence

- `cd web && npm run build` — type-check + build green (`SequenceDetail` chunk
  rebuilt).
- `npx eslint src/pages/sequence/SequenceBlocks.tsx src/pages/sequence/SequenceDetail.tsx`
  — clean.
- Diff audit: only `SequenceDetail.tsx` (−19/+3) and the new `SequenceBlocks.tsx`
  are dirty.
