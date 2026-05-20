# BLAST Submit Search Set Table

## Motivation

The Choose Search Set step looked like a plain database path input when the local database list was unavailable, making the selected search set hard to scan and compare against the design proposal.

## User-facing change

- Replaced the database dropdown path presentation with a table-style selector showing Database, Type, Size, and Status.
- Kept downloaded database rows selectable and preserved the NCBI catalogue gap rows as disabled reference rows.
- Added a table-style fallback for manual database paths so local/degraded database-list states still show the selected search set clearly.
- Hid the duplicate selected storage path text/input once a database is selected; the manual path input is only shown when no search set is selected.
- Reduced the Program Selection tab height in the light submit layout from 81px to 60px for a tighter first step.
- Aligned the light submit body grid to the same outer width as the header by removing the grid's extra horizontal padding.
- Offset the sticky left stepper and right summary rail below the sticky topbar so they no longer climb awkwardly to the viewport top while scrolling.
- Added a subtle success/info tint to the query FASTA stats strip and semantic button colors for upload, example loading, sequence transform, deduplication, and clear actions.
- Refined the taxonomy filter modal in light mode with softer empty states, clearer preview/detail cards, a lighter footer action bar, and a more intentional filter-mode surface.
- Added popular taxonomy quick-pick chips to the modal's empty search state so it starts with useful choices instead of a mostly blank column.
- Removed the duplicate filter-mode control from the taxonomy modal; include/exclude remains available in the main Taxonomy Filter section.
- Added zoom controls to the taxonomy lineage tree so expanded all-rank trees remain readable and can be panned in a scrollable canvas.
- Restyled the submit rail pre-flight readiness result as a proposal-aligned status card and changed the command preview to preserve command tokens with horizontal scrolling instead of breaking words mid-flag.
- Moved the submit rail job title out of the right-aligned key/value row into a full-width wrapped line so long titles are shown completely.

## API/IaC diff summary

No API or IaC changes. This is a frontend-only change in the BLAST submit page and theme styles.

## Validation evidence

- `npm run build` in `web/` completed successfully on 2026-05-20.
- Browser snapshot at `http://127.0.0.1:8090/blast/submit` showed the Choose Search Set section with `Database`, `Type`, `Size`, `Status`, `core_nt`, `nucl`, `~250 GB`, and `Selected · manual`, without the duplicate selected path line/input.
