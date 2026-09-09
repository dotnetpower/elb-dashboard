---
title: Restore saved BLAST submit templates
description: Restore the existing owner-scoped template control while enforcing that query and execution identity data cannot be persisted or applied.
tags: [blast, ui, security]
---

# Restore saved BLAST submit templates

## Motivation

The saved-template API, owner-partitioned persistence, typed client, and UI control already existed, but the control had been unmounted from New Search. Researchers could not reuse named scientific parameter sets through the browser.

## User-facing change

New Search again displays **Submit templates**. A researcher can save, apply, and delete named parameter presets. Applying a preset updates reusable scientific options while preserving the current FASTA or accession, query range, job title, and selected cluster.

Templates remain scoped to the authenticated caller and do not alter submission, admission, request-queue, or completion-topic behavior.

## API and security

The existing `GET`, `POST`, `PUT`, and `DELETE /api/blast/templates` routes and response shapes are unchanged. The write boundary now rejects known query, per-run, and execution-identity keys, including inline FASTA, accession, blob/file references, query ranges, job title, idempotency key, and external correlation ID. Existing size, key-count, name, owner partition, and template-count limits remain unchanged.

Persistence accepts only the known reusable form fields with finite scalar
values. Legacy rows are filtered through the same projection on read. Storage
outages return 503 and render an accessible error instead of appearing as an
empty template list; create and delete failures remain visible to the user.

Field-specific types, ranges, and critical enums are checked. Query and subject
input flags hidden inside `additional_options` are rejected at the API and
removed by the UI before save, including quoted values. Lists remain capped at
50 rows even if legacy data bypassed the normal create limit, and rename uses
the same duplicate-name rule as create.

No infrastructure, Azure role, Service Bus setting, submit payload, queue consumer, or completion subscriber changed.

## Validation

- `uv run pytest -q api/tests/test_blast_templates.py` - 30 passed.
- `npm --prefix web test -- --run src/pages/blastSubmit/BlastTemplatesControl.test.ts src/api/pathContracts.test.ts` - 5 passed.
- `npm --prefix web run build` - passed; only the pre-existing Rollup chunk-size warning remained.
- Playwright verified the unavailable state against blocked local Storage, then
	a mocked saved template at 1,440 x 1,000 and 390 x 844. Applying it changed
	reusable parameters while preserving the current FASTA and job title.
- The final expansion validation rechecks the complete Service Bus suite and protected source hashes against the pre-expansion baseline.
