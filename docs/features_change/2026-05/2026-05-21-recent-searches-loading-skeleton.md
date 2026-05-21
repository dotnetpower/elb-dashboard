# 2026-05-21 — Recent searches loading skeleton

## Motivation

The Recent searches page previously rendered generic loading bars while the
BLAST job history request was in flight. The loading state did not communicate
the final table structure, so the page felt visually disconnected from the
loaded job list.

## User-facing change

The `/blast/jobs` page now shows an animated skeleton that mirrors the Recent
searches table: date group heading, Job/User/Status/Time/Delete columns, job
title lines, metadata lines, status badges, and time cells.

## API and UI diff summary

| Area                    | Change                                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------------------ |
| Recent searches loading | Replaces generic row bars with table-shaped skeleton rows.                                             |
| Accessibility           | Marks the loading table as a polite busy status region.                                                |
| Responsive behavior     | Reuses the existing Recent searches table layout so the skeleton follows the same mobile column rules. |

## Validation evidence

- `cd web && npm run lint -- --quiet`
- `cd web && npm run build`
- Browser smoke confirmed `/blast/jobs` renders animated table-shaped skeleton rows while `/api/blast/jobs` is pending.
