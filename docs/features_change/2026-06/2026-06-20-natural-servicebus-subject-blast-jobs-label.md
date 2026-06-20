---
title: Natural Service Bus request subjects and "BLAST Jobs" menu label
description: Distinguishable request-queue message subjects and a clearer BLAST Jobs navigation label.
tags:
  - blast
  - ui
---

# Natural Service Bus request subjects + "BLAST Jobs" menu label

## Motivation

Two small usability papercuts:

1. **Service Bus request messages were indistinguishable.** Every BLAST request
   enqueued onto the request queue carried the constant Subject
   `blast.request`, so an operator inspecting the queue (Azure portal / Service
   Bus Explorer, or the dashboard Playground / Message Flow peek) could not tell
   one queued job from another.
2. **The "Recent searches" menu label read awkwardly.** The navigation item for
   the job history list is really the BLAST jobs roster, so "Recent searches"
   was unclear.

## User-facing change

- **Request-queue message Subject is now natural and distinguishable.** It is
  composed from the request itself: `"{program} {db}"` plus the first query
  defline derived from the inline FASTA. Examples:
  - `blastn core_nt · sp|P12345 (+2)`
  - `blastp nr`
  - `blast.request` — preserved fallback when the body carries no
    program/db/query identity.
- **"Recent searches" is now "BLAST Jobs"** in the sidebar navigation, the
  breadcrumb, the job-detail back link, and the job-list page header.

The Subject is identification-only — the consumer never routes or filters on it
(the drain/requeue path preserves whatever Subject it sees and falls back to
`blast.request`), so this cannot affect drain behaviour.

## API / IaC diff summary

- New `api/services/blast/request_subject.py` — pure `build_request_subject(body)`
  helper (reuses `derive_inline_query_label`, never raises, length-capped at 120).
- `api/services/blast/submit_ingress.py` — the unified-ingress producer now passes
  `subject=build_request_subject(body)` to `send_request`.
- `api/routes/settings/service_bus.py` — the Playground send now passes
  `subject=build_request_subject(payload)` instead of the hardcoded
  `"blast.request"`.
- Web: `Layout.tsx`, `Breadcrumb.tsx`, `BlastJobHeader.tsx`, `JobsHeader.tsx`
  label text "Recent searches" / "Recent BLAST searches" → "BLAST Jobs".

## Validation evidence

- `uv run pytest -q api/tests/test_request_subject.py` — 8 passed (new suite).
- `uv run pytest -q api/tests/test_submit_ingress.py api/tests/test_settings_service_bus.py api/tests/test_service_bus_drain_loop.py api/tests/test_servicebus_tasks.py api/tests/test_service_bus_peek.py api/tests/test_blast_submit_route_options.py` — 102 passed.
- `uv run ruff check` on the touched backend files — clean.
- `cd web && npx eslint <touched tsx>` — clean; `npm run build` — built successfully.
