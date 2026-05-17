# Taxonomy Filter Modal — Variant B Visual Hardening

## Motivation
The new split-panel `TaxonomyModal` (Variant B mockup) had two ship-blocking defects
when validated end-to-end in the browser at `/blast/submit`:

1. **State-reset bug** — Selecting a row from the results list flashed back to the
   empty placeholder. The detail panel never hydrated.
2. **Detail panel rendered centered** — Even though the parent column resolved to
   `text-align: left`, every text element inside `.taxonomy-modal__detail`
   computed to `center`, leaving the lineage / metadata grid visually broken.

The image, scroll behaviour, backdrop tint, and empty-state padding also needed
polish before the modal looked acceptable on a 1366×768 laptop.

## User-facing change
- The detail panel now hydrates on the first click and persists across re-renders.
- Detail panel text (name, common-name line, Wikipedia link, lineage, metadata
  grid) is consistently left-aligned.
- Lineage block caps at `88px` and exposes a thin custom scrollbar instead of
  pushing the metadata grid off-screen.
- Wikipedia thumbnail rail is `108px` tall with `flex-shrink: 0` so the layout
  stops jumping when the image loads.
- Backdrop darkened to `rgba(2, 4, 10, 0.62)` with a `2px` blur so the modal
  feels modal-grade against the dashboard underneath.
- Recent chips, exclude/include toggle, and the footer command preview were
  re-verified in the browser (recent persists, `-taxids` ↔ `-negative_taxids`
  updates immediately).

## Code diff summary
- `web/src/pages/blastSubmit/TaxonomyModal.tsx`
  - Hydration `useEffect` deps switched from `[open, initial]` (object identity
    flipped every parent render) to primitive field deps:
    `[open, initial.taxid, initial.taxid_label, initial.taxid_rank, initial.is_inclusive]`,
    with a single `react-hooks/exhaustive-deps` suppression because the lint
    rule cannot see through the destructure.
- `web/src/theme/glass.css` (taxonomy-modal block)
  - `.taxonomy-modal__backdrop`: darker tint + `backdrop-filter: blur(2px)`.
  - `.taxonomy-modal`: clamped `max-height: min(calc(100vh - 32px), 680px)` and
    explicit `text-align: left`.
  - `.taxonomy-modal__detail`: explicit `text-align: left !important` plus a
    compound `:not(.taxonomy-modal__detail--empty)` override on direct children.
    The `!important` is intentional — the cascade was producing `center` from
    no matching rule on this exact host, so we force the safe alignment.
  - `.taxonomy-modal__detail-image`: height `140px → 108px`, `flex-shrink: 0`.
  - `.taxonomy-modal__detail--empty`: tightened padding/gap, `min-height: 160px`.
  - `.taxonomy-modal__detail-lineage`: `max-height: 88px; overflow-y: auto;`
    plus thin custom scrollbar (Firefox + WebKit).
  - `@media (max-width: 760px)`: image `100px`, modal `max-height`
    `calc(100vh - 32px)`.

No API, IaC, or Bicep changes.

## Validation evidence
- `cd web && npm test -- --run` → **12 files, 87 tests passed** (includes the
  17 `useRecentTaxonomy.test.ts` cases that already covered the hook).
- `cd web && npx eslint src/pages/blastSubmit/TaxonomyModal.tsx src/pages/blastSubmit/useRecentTaxonomy.ts`
  → clean. (The two pre-existing `react-hooks/exhaustive-deps` warnings in
  `EndpointCard.tsx:36` and `useDbWithWarmupPlan.ts:96` are on `main` and not
  introduced by this change.)
- `cd web && npm run build` → **vite build green in 8.40s**.
- Browser smoke at `http://127.0.0.1:8090/blast/submit`:
  - Searched `Homo sapiens` → first row click hydrated the detail panel
    (`getComputedStyle('.taxonomy-modal__detail').textAlign === 'left'`).
  - Toggled `Exclude` → footer preview updated `-taxids 9606` → `-negative_taxids 9606`.
  - Clicked `Apply`, re-opened the modal → `Recent` chip showed
    `Homo sapiens 9606`, exclude mode persisted, detail auto-hydrated from
    the recent.
  - Lineage block scrolls inside the detail card without spilling.
