# Make the BLAST database category radio look like the NCBI radio (and behave like it)

## Motivation
A user opened the BLAST submit page with NCBI's classic UI side-by-side and
asked why our dashboard appeared to "lock" the database to `core_nt`.

In reality the page already exposes the same four NCBI-style categories
(Standard / rRNA·ITS / Genomic+transcript / Custom) and the rRNA tab
already had three downloaded databases (`16S_ribosomal_RNA`,
`18S_fungal_sequences`, `ITS_RefSeq_Fungi`). Two affordance gaps made the
tabs look decorative instead of interactive:

1. **No radio dot.** The four labels rendered as low-contrast pill buttons,
   easy to mistake for chips or counters rather than a radio group.
2. **Off-category selections leaked into the dropdown.** When the user
   switched to `rRNA/ITS`, the previously selected `core_nt` was prepended
   to the dropdown so the box still read "Core Nucleotide". That hid the
   category swap and reinforced the "stuck on core_nt" impression. NCBI's
   own UI clears the previous pick the moment a different category is
   selected.

## User-facing change
- The category buttons now show an obvious NCBI-style radio dot (filled
  blue when active) and the active tab has a stronger background +
  shadow so its state is unambiguous from across the screen.
- Clicking a different category clears the dropdown when the previous
  selection does not belong to the new category. The dropdown then shows
  only the databases that actually live in that category.
- The active category is now **derived from the current `form.db`** on
  mount, so reopening a draft that referenced `16S_ribosomal_RNA` lands
  the user on the `rRNA/ITS` tab — not on `Standard databases`.

No backend, infra, or schema changes.

## Diff summary
- `web/src/pages/blastSubmit/DatabaseSection.tsx`
  - new `deriveCategoryFromForm()` helper.
  - initial `useState<SearchSetCategory>` derived from `form.db`.
  - `useEffect` re-syncs the tab when `databases`/`form.db` change
    (and only then; manual clicks are preserved).
  - new `handleCategoryChange()` clears `form.db` when the previous
    selection does not belong to the chosen category.
  - `visibleDatabases` no longer prepends an off-category current entry.
  - tab markup adds a leading `<span class="blast-search-set-tab__radio" />`.
- `web/src/theme/glass.css`
  - `.blast-search-set-tab__radio` styles (empty ring → filled blue dot).
  - stronger active border / background / inset shadow.
  - count badge now rendered as a small accent-tinted pill on the right.

## Validation
- `cd web && npx tsc --noEmit` → clean.
- `cd web && npx eslint --max-warnings 0 src/pages/blastSubmit/DatabaseSection.tsx`
  → clean.
- `cd web && npm run build` → built in ~11 s.
- Manual (Playwright + screenshot): the `Standard databases` tab opens
  with the filled radio dot and the dropdown listing `core_nt` +
  `elb_compare_tiny`. Clicking `rRNA/ITS databases` flips the dot, clears
  the dropdown to `— Select a database —`, and offers exactly the three
  rRNA databases that exist in storage.
