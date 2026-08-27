---
title: Dashboard functional audit hardening
description: Fix narrow-screen navigation overflow, restore the NCBI accession input label contract, and expand browser workflow coverage.
tags: [ui, contributor]
---

# Dashboard functional audit hardening

## Motivation

The dashboard's existing desktop workflow coverage did not exercise the BLAST database manager on phone-sized screens or directly enter every lazy-loaded route. That gap hid a horizontal page overflow in the mobile top bar and an NCBI accession input whose visible text was not programmatically associated with the field.

## User-facing change

- The dashboard top bar now stays within 320-390 px mobile viewports, so the user menu remains reachable without horizontal page scrolling.
- The **Or fetch by NCBI accession** text is now a real input label, restoring label-click, assistive-technology, and automation access to the field.
- The BLAST database manager keeps the **Auto oracle** control visible and usable on a 390 px viewport.
- Invalid warmup requests now return `422 invalid_warmup_request` before creating JobState or contacting the Celery broker.

## API and infrastructure summary

- `POST /api/warmup/start` now validates its six required scope fields and `num_nodes` before any durable or queued side effect. Valid dashboard payloads are unchanged.
- No infrastructure contract changed.
- The [Playwright](https://playwright.dev/) safe suite now covers Auto oracle preference toggling, Diagnostics report rendering, Service Bus dry-run validation, Sequence Detail to New Search handoff, the mobile database modal, and the 320 px navigation drawer.
- The mock Auto oracle preference endpoint now persists writes for subsequent refetches, matching the live API's read-after-write behavior.

## Validation

- `npm --prefix web run test`: 109 files and 982 tests passed.
- `npm --prefix web run lint`: passed with zero warnings.
- `npm --prefix web run build`: passed.
- `scripts/dev/local-run.sh smoke`: 27/27 API probes passed, including the invalid warmup fast-fail.
- The complete safe Playwright suite passed with 51 tests and 6 explicitly guarded live-mutation skips. It covers the 320 px header, 390 px database manager, Auto oracle save/refetch, Diagnostics, Service Bus Playground, and Sequence Detail handoff.
- The deployed control plane rendered 18 routes and deep links with no console error, page exception, failed request, HTTP error response, or error boundary. Live mutations remained excluded.