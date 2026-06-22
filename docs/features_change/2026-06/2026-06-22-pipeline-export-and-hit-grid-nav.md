---
title: Pipeline export menu + Descriptions keyboard grid navigation
description: Frontend wiring for the per-job workflow-manager export (#57 R3) and ARIA-row keyboard navigation on the Descriptions hits table (#30), plus the Pipeline Export user-guide page.
tags:
  - ui
  - blast
---

# Pipeline export menu + Descriptions keyboard grid navigation

## Motivation

Two researcher-facing gaps remained on the BLAST result page:

- **#57 (roadmap R3)** — the backend already renders Nextflow / Snakemake / CWL /
  WDL modules (`GET /api/blast/jobs/{id}/export`), but the SPA had no way to
  download them, so a researcher still could not slot a dashboard search into a
  pipeline without the CLI.
- **#30** — the Descriptions hits table (which can hold thousands of windowed
  rows) had no keyboard row navigation, so keyboard/screen-reader users could
  only Tab through every interactive cell.

## User-facing change

- **Pipeline export ▾** menu on the result header (next to *Copy citation*).
  Picking Nextflow / Snakemake / CWL / WDL downloads the self-contained module
  (`main.nf` / `Snakefile` / `blast_submit.cwl` / `blast_submit.wdl`) that
  re-submits the job's exact parameters via one `POST /api/blast/jobs` call.
  The download uses the server's `Content-Disposition` filename; a job with no
  recorded parameters surfaces a clear "no recorded parameters to export"
  message (422) instead of a silent failure.
- **Keyboard navigation** on the Descriptions hits table: Up/Down move the
  focused row (roving `tabindex`), Home jumps to the first row, End to the last
  *loaded* row. Pressing Down on the last painted row when more rows exist grows
  the #29 row window by one batch and moves focus into it — so keyboard users
  reach the whole result set without a mouse.
- New **Pipeline Export** user-guide page documenting the end-to-end loop
  (Export → pipeline runner → `POST /api/blast/jobs` → results in the same
  workspace), including the `ELB_BASE_URL` / `ELB_TOKEN` / `ELB_QUERY_FASTA`
  runtime contract.

## Implementation summary

- `web/src/api/blast.types.ts` — new `WorkflowExportFormat` union (mirrors the
  backend `SUPPORTED_WORKFLOW_FORMATS`).
- `web/src/api/blast.ts` — `getWorkflowExport(jobId, format)` (text download via
  the existing `api.getText`, carrying the bearer; no SAS, no token in the file).
- `web/src/pages/blastResults/workflowExportModel.ts` (+ test) — pure format
  metadata + `workflowExportFilename` fallback, with a guard test pinned to the
  backend `_FORMAT_FILENAMES`.
- `web/src/pages/blastResults/BlastJobHeader.tsx` — `PipelineExportMenu`
  dropdown (click-outside / Esc close, `role=menu`), `handleWorkflowExport`
  (in-flight guard, 422-vs-transient error toasts), and a text-blob download
  helper.
- `web/src/pages/blastResults/analytics/hitGridNav.ts` (+ test) — pure
  `computeHitGridFocus` reducer for Arrow/Home/End with the window-load seam,
  exhaustively unit-tested (empty set, first/last row, load seam, full walk with
  no skips/repeats, out-of-range clamp).
- `web/src/pages/blastResults/analytics/BlastHitsTable.tsx` — roving-tabindex
  rows, `onKeyDown` handler (skips when focus is in a text input / textarea /
  select / contenteditable so it never hijacks a real control), focus-on-paint
  effect gated by a pending flag so unrelated re-renders never steal focus, and
  consistent `aria-rowcount` / `aria-rowindex` (header row = 1).
- `docs/user-guide/pipeline-export.md` + `mkdocs.yml` nav entry.

The native `<table>` semantics are intentionally preserved (implicit
`row`/`cell`/`columnheader`/`rowgroup` roles) rather than overriding them with an
explicit `role="grid"`, which would require re-roling every cell and risks an
assistive-technology regression. The interactive keyboard layer is purely
additive.

## Design critique (self-critique rubric) + 5 hardening rounds

- **Contract:** `BlastHitsTable` / `BlastJobHeader` public props unchanged — no
  consumer (DescriptionsTabBody, BlastResults) needed edits. New
  `WorkflowExportFormat` type and `getWorkflowExport` method are additive.
- **Liveness/loops:** the window-grow on ArrowDown is bounded by
  `Math.min(current + ROW_STEP, hits.length)`; ArrowDown on the true last row is
  a no-op. No unbounded loop.
- **Idempotency/concurrency:** `handleWorkflowExport` early-returns while an
  export is in flight and menu items are disabled, so a double-click cannot fire
  two downloads.
- **Partial failure:** export errors degrade to a toast (422 = permanent "no
  parameters", else transient) — never an unhandled rejection.
- **Security:** the export route is `require_caller`-protected; the download
  carries the bearer via `api.getText` and the rendered module embeds no SAS /
  token / storage URL.
- **Backward-compat:** all additions are optional/additive; no field removed or
  renamed.

Hardening rounds applied: (1) consistent header `aria-rowindex=1`; (2) grid
nav skips arrow-consuming form controls; (3) 422-vs-transient export error
messages; (4) reset the pending-focus flag when the hit set changes; (5) no-op
key presses consume the key (no page scroll) without state churn.

## Remaining gate (#30)

Keyboard navigation + the windowing seam are implemented and unit-tested, but a
**screen-reader pass** (NVDA / VoiceOver) to confirm the row announcements and
the load-more focus move read correctly is still required before #30 closes —
that verification cannot run in a code session, so #30 stays open for it.

## Validation

- `cd web && npx vitest run` — 945 passed (16 new: `workflowExportModel` 5,
  `hitGridNav` 11).
- `cd web && npm run build` — clean (tsc + vite).
- `cd web && npx eslint <touched>` — clean.
- `uv run pytest -q api/tests/test_blast_citation.py
  api/tests/test_blast_workflow_export.py` — 29 passed (backends unchanged).
