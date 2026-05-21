# Alignments tab — infinite scroll for pairwise cards

## Motivation
Recent searches → **Alignments** mounted every `AlignmentViewer` in the
current page at once. Each viewer renders one styled `<span>` per base in
`qseq`/`sseq` (~462 bases × Q + S + match line), so a single page of 100
hits produces ~100k DOM nodes. First paint on the Alignments tab was the
slowest part of the result view, and the existing page-size knob (100)
was already a soft cap users disliked lowering.

## User-facing change
* The Alignments tab now mounts only the first 10 pairwise cards on
  initial render and incrementally mounts 10 more as the user scrolls
  toward the bottom (IntersectionObserver, 400 px pre-fetch buffer).
* A muted "Showing X of Y — loading more…" sentinel appears while more
  cards remain; once all cards are visible it switches to "All N
  alignments shown." (only shown when N > initial batch).
* Server pagination, filters, sort, and degraded banners are unchanged.
  The infinite-scroll window resets on alignments-array identity change
  (page change, filter apply, refetch).
* SSR / no-`IntersectionObserver` fallback: the component renders the
  full list immediately, preserving prior behaviour.

## API / IaC diff summary
None. The change is purely client-side; the existing
`/api/blast/jobs/{id}/results/alignments` paged endpoint is unchanged.

## Validation
* `cd web && npm run build` → clean, no type errors.
* `npx eslint src/pages/blastResults/analytics/AlignmentsTabBody.tsx`
  → clean.
* Verified the existing pagination flow still works (page change resets
  the visible window via the `useEffect([alignments])` reset).
