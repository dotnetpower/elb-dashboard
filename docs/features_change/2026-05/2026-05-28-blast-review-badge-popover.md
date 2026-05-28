# BLAST Results: Review badge popover + column hint tooltips

## Motivation

The Descriptions tab labels each hit with a one-word "Review" badge (Strong /
Review / Low / Weak / Unknown) but the underlying classification rule is
invisible to the user. NCBI Web BLAST veterans were also confused by the
**HSP Cover** column because the same name ("Query Cover") at NCBI is the
*per-subject union* of all HSPs, whereas ours is *per-HSP* coverage. % Identity
cell colors and the Max / Total bit score pair shared the same problem on a
smaller scale: there was no surfaced legend.

## User-facing change

1. **Review badges are now popover triggers.** Hover, focus, or click the badge
   to reveal:
   - The current tier label + the one-line reason.
   - This row's actual `% identity / HSP cover / E-value` values.
   - A 5-row threshold table with the active tier highlighted.
   - If the row is `Unknown`, an explicit "Missing field(s): …" message.

   Closes on Esc, mouse-leave, blur, or outside-click. Keyboard accessible
   (`<button>` trigger with proper `aria-haspopup="dialog"` / `aria-expanded`).

2. **Column header (?) tooltips** on four columns:
   - **Review** — what the classification is and that the per-row badge
     explains thresholds.
   - **HSP Cover** — defines per-HSP coverage and explicitly calls out the
     difference vs NCBI Web BLAST's `Query Cover`.
   - **% Identity** — defines pident and explains the cell color bands
     (≥90 green, ≥70 amber, <70 red).
   - **E-value** — one-sentence definition + the `1e-20` / `1e-5` rules of
     thumb.
   - **Max / Total** — promotes the existing plain `title=` text into a rich
     tooltip ("Max = this HSP, Total = sum across every HSP for this subject").

   Sort behaviour is unchanged — clicks on the (?) icon do not steal the
   header's sort click thanks to a `stopPropagation` wrapper.

## API / IaC diff summary

- Frontend only. No backend, schema, or IaC changes.
- The classifier (`api/services/blast/result_analytics.py::annotate_result_hit`)
  remains the source of truth; the new
  `web/src/pages/blastResults/analytics/reviewBadgeMeta.ts` mirrors its
  thresholds and is guarded by a snapshot test so a backend tweak forces an
  immediate frontend update.

### Files changed

- New: `web/src/pages/blastResults/analytics/reviewBadgeMeta.ts`
- New: `web/src/pages/blastResults/analytics/ReviewBadgePopover.tsx`
- New: `web/src/pages/blastResults/analytics/reviewBadgeMeta.test.ts`
- Modified: `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` — wires
  `ReviewBadgePopover`, adds `hint` prop to `SortableHeader`, attaches the
  four column tooltips. The legacy inline `ReviewBadge` function and its
  duplicate label/color tables were removed.
- Modified: `web/src/theme/glass.css` — adds the `.review-badge`,
  `.review-popover`, and `.rp-table` selectors (~164 lines, all under their
  own namespaces; `.tooltip-popup` is untouched).

## Validation evidence

- `cd web && npx vitest run` → **52 files / 389 tests passed**, including the
  new 6 `reviewBadgeMeta.test.ts` cases (tier ordering, exact thresholds vs
  backend constants, label uniqueness, missing-field detection).
- `cd web && npm run build` → My changes compile clean. (The 3 errors reported
  by `tsc -b` belong to in-progress vnet-peering work in
  `web/src/components/SettingsPanel.tsx` and are unrelated to this change.)
- Manual smoke against the screenshot in the originating chat: the three
  example rows (`NR_024570.1` → Strong, `NR_074902.1` / `NR_026331.1` →
  Review) now expose the exact reason and per-field values on hover, and the
  threshold table highlights the matching row.
