---
title: BLAST results table explains why Description / HSP Cover columns are blank
description: A "7 std staxids sscinames" run omits stitle and qlen, so the Description and HSP Cover columns are necessarily blank; the table now shows a one-line reason banner instead of leaving the user guessing.
tags:
  - blast
  - ui
---

# BLAST Results — Empty-Column Reason Banner (#32)

## Motivation

When a BLAST search uses `-outfmt 7 std staxids sscinames` (the dashboard's
"Mode B core_nt tabular + taxids" path), the result table's **Description** and
**HSP Cover** columns are necessarily blank, because that specifier carries no
source data:

- **Description** renders `stitle` (subject title) — not in `std` or the
  taxonomy columns.
- **HSP Cover** renders `qcovs`, which the backend derives from `qlen` (query
  length) — `std` carries no `qlen`.

This is correct given the chosen outfmt, but the columns were present yet always
empty with no explanation. NCBI Web BLAST always shows Description + Query Cover,
so a researcher reasonably reads the blanks as missing data.

## User-facing change

When **every** hit in the result set has no `stitle` (Description) and/or no
derivable `qcovs` (HSP Cover), the results table renders a one-line note above
the table:

> **Description and HSP Cover are blank for this run.** This search used a
> tabular output format (e.g. `7 std staxids sscinames`) that does not include
> `stitle` (Description) or `qlen` (needed for HSP Cover). Re-run with
> `stitle qlen` appended to the outfmt specifier, or use `outfmt 5` (XML), to
> populate these columns.

The banner only shows when the whole result set is blank for that column, so a
genuinely populated run (e.g. `outfmt 5` XML, which always carries both) never
sees it.

## API / IaC diff summary

Frontend-only, presentational:

* [web/src/pages/blastResults/analytics/BlastHitsTable.tsx](../../../web/src/pages/blastResults/analytics/BlastHitsTable.tsx)
  — compute `descriptionColumnEmpty` / `coverColumnEmpty` from the hit set and
  render a `role="note"` banner when either is whole-set blank.

No backend, schema, or outfmt change. (Changing the default preset outfmt was
considered — option 1 in the issue — but is deferred so we do not silently widen
every taxonomy run's column set.)

## Validation evidence

* `cd web && npm run build` → success.
* `npx vitest related --run …BlastHitsTable.tsx` → 87 passed (incl. existing
  `BlastHitsTable.test.ts`).
