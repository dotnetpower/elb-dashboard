---
title: BLAST jobs list — copyable job_id chip on each row
description: >-
  Each row in the BLAST jobs list now shows a monospace job_id chip that copies
  the full id on click, so jobs can be correlated with logs and Service Bus.
tags:
  - blast
  - ui
---

# BLAST jobs list — copyable `job_id` chip

## Motivation

The jobs list row showed a human title (query/db) but only fell back to the raw
`job_id` when no title existed. During load tests and Service Bus / OpenAPI
correlation, operators need the actual `job_id` visible and copyable without
opening the detail page.

## User-facing change

- Each [JobRow](web/src/pages/BlastJobs/JobRow.tsx) now renders a small
  monospace `id <prefix…suffix>` chip in the row metadata line.
- UUID ids (36 chars) are collapsed to `<8 char prefix>…<4 char suffix>` so the
  row stays compact; short OpenAPI ids (≤14 chars) show in full.
- Clicking the chip copies the **full** `job_id` to the clipboard
  (`navigator.clipboard.writeText`); the full id is also in the chip `title`.
- The click is `preventDefault`/`stopPropagation`-guarded so it never triggers
  the row's navigation to the job detail page.

## Diff summary

- [web/src/pages/BlastJobs/JobRow.tsx](web/src/pages/BlastJobs/JobRow.tsx):
  added a `shortJobId` helper and the chip button. No backend change — `job_id`
  was already present on every jobs-list payload row.

## Validation

- `npx eslint src/pages/BlastJobs/JobRow.tsx` — clean.
- `npm run build` — succeeds.
- `npx vitest run src/pages/BlastJobs` — 6 passed.
