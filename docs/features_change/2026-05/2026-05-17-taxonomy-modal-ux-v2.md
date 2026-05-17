# Taxonomy filter UX overhaul (v2)

**Date:** 2026-05-17
**Scope:** `web/` — BLAST Submit page taxonomy filter section + modal
**Backend impact:** none (uses existing `/api/blast/taxonomy/{search,detail/{id},image}` routes)

## Motivation

Four concrete pain points raised on the BLAST Submit page:

1. **Recent taxa were buried in the modal.** Users had to open the picker just to flip back to a recent organism. The filter card needed a one-click reuse path.
2. **Trigger button was vertically misaligned.** Because the launcher used `align-items: center` against a two-line label + selected-box stack, the "Change taxon" button floated mid-stack instead of sitting on the baseline of the selection box.
3. **Modal was cramped and cropped images.** The 840 px / 2-column modal forced the Wikipedia thumbnail into a 108 px-tall slot with `object-fit: cover`, so the organism photo was always cropped. Empty/missing images showed a colourless "no image" placeholder.
4. **Autocomplete was query-only, never suggestive.** Typing fired a 400 ms-debounced E-utilities search. Even short typos returned nothing until the live call landed, which made the field feel sluggish and rarely surfaced popular research targets.

## User-facing change

### Filter card (`TaxonomyFilterSection`)

- Recently used taxa now appear as **quick-pick chips directly under the launcher** (no modal hop). Clicking a chip applies the filter with the prior inclusive/exclusive setting and bumps it to the head of the recent list.
- When there is no recent history yet, the row falls back to a curated **Popular** strip (Homo sapiens, Mus musculus, Drosophila, …) so the section is useful on first load.
- Launcher layout: the "Change taxon" button is now `align-self: flex-end`, baselined against the selection box.

### Modal (`TaxonomyModal`)

- **3-column layout** (was 2):
  1. Search + recent + merged suggestions listbox.
  2. **New dedicated image column** with `object-fit: contain` (up to 280 px tall) and a Wikipedia link in the caption.
  3. Lineage / metadata grid + filter mode toggle.
- **Local-first autocomplete.** A new curated catalog of ~40 popular taxa (`taxonomyCommon.ts`) is filtered client-side on every keystroke and shown immediately, ahead of any debounced live search. The first item auto-focuses so the detail panel and image load instantly. Live E-utilities results merge in below, deduped by taxid.
- Suggestion rows show a small **`Sparkles` badge + "curated" tag** when the row is in the curated set, so users see at a glance that the recommendation is well-known.
- **SVG fallback icon** (`TaxonomyDefaultIcon`, a stylised phylogenetic tree) replaces the previous `ImageOff` rectangle for organisms without a Wikipedia thumbnail. It also fills the image column when nothing is selected, so the column reads as intentional empty-state.
- Modal width is now `min(1180px, calc(100vw - 32px))` (was 840 px). The mobile breakpoint dropped from 760 px → 860 px so 3 columns survive on common laptops (978 px and up).

### Screenshot

![Taxonomy modal — 3 columns, curated suggestions, image preview](assets/2026-05-17-taxonomy-modal-3col.png)

## API / IaC diff summary

None. Only `web/` changed; the modal reuses the existing endpoints:
- `GET /api/blast/taxonomy/search?q=&limit=`
- `GET /api/blast/taxonomy/detail/{taxid}`
- `GET /api/blast/taxonomy/image?name=`

## Files touched

| File | Change |
| --- | --- |
| `web/src/pages/blastSubmit/taxonomyCommon.ts` | NEW — curated `CommonTaxon` catalog (~40 entries) + `filterCommonTaxa` / `topCommonTaxa` / `getCommonTaxon` helpers. |
| `web/src/pages/blastSubmit/TaxonomyDefaultIcon.tsx` | NEW — themable SVG fallback (phylogenetic tree). |
| `web/src/pages/blastSubmit/TaxonomyFilterSection.tsx` | Quick-pick chip row (recent/popular), button alignment cleanup, `Star`/`History` icon imports. |
| `web/src/pages/blastSubmit/TaxonomyModal.tsx` | 3-column body, `mergedResults` (curated ∪ live, deduped), `TaxonomyImagePanel` extracted, `TaxonomyDetailPanel` simplified (image moved out), `Sparkles` "curated" badges, focus walks merged list. |
| `web/src/theme/glass.css` | New `.taxonomy-modal__image*`, `.taxonomy-modal__result-dot--curated`, `.taxonomy-modal__result-badge`, `.taxonomy-modal__section-badge`, `.taxonomy-filter-quickpick*`; updated `.taxonomy-modal__split` to 3 columns, modal width 1180 px, launcher alignment `flex-end`, breakpoint at 860 px. |

## Validation evidence

- **Unit tests**: `cd web && npm test -- --run` → **12 files / 87 tests pass** (incl. `useRecentTaxonomy.test.ts` 17/17 and `taxonomyFilter.test.ts` 8/8). No new tests required — quick-pick chips reuse `useRecentTaxonomy` and the merge logic is exercised end-to-end in the browser.
- **Lint**: `npx eslint src/pages/blastSubmit/TaxonomyModal.tsx src/pages/blastSubmit/TaxonomyFilterSection.tsx src/pages/blastSubmit/taxonomyCommon.ts src/pages/blastSubmit/TaxonomyDefaultIcon.tsx --max-warnings 0` → clean (no output).
- **Build**: `npm run build` → `dist/assets/index-*.js  679.53 kB` in 5.00 s, no errors.
- **Browser smoke** (Playwright MCP against local dev at `http://127.0.0.1:8090/blast/submit`):
  - Filter card renders the launcher with the "Change taxon" button baseline-aligned and a quick-pick chip for the existing recent `Homo sapiens (9606)` filter.
  - Modal opens at 1180 px width with three visible columns.
  - Typing `mus` → instantly shows curated **Mus musculus** with the `curated` badge before the live API responds; detail + Wikipedia image populate from the cached curated entry within the same render. Once the live search lands, two more `Mus`-prefix taxa append below the curated row (header reads "Suggestions · 3 — 1 curated"). API trace from the dev api confirms `GET /api/blast/taxonomy/search?q=mus&limit=8 200` and `GET /api/blast/taxonomy/image?name=Mus%20musculus 200` firing alongside the local match.
  - Typing a numeric taxid (`3431483`) routes through the live search and renders the resulting Orthopoxvirus monkeypox entry with the SVG fallback icon when no Wikipedia thumbnail exists.

## Notes / follow-ups

- `taxonomyCommon.ts` priorities are hand-curated. If we add usage telemetry later we can replace `topCommonTaxa` with a "most clicked across all users" feed without touching the modal.
- The `.taxonomy-modal__detail-link` rule in `glass.css` is now dead (Wikipedia link moved into the image caption). Left in place because purging unused CSS is out of scope for this change; will be removed in the next pass on this file.
