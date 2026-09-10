---
title: Expose sequence diversity in API tools
description: Add sequence-diversity options to API tools and harden their validation, state, accessibility, and wire contracts through 25 review rounds.
tags:
  - blast
  - ui
---

# Expose sequence diversity in API tools

## Motivation

The typed [OpenAPI](https://spec.openapis.org/oas/latest.html) request contract and
Service Bus Playground accepted `sequence_diversity`, but the API Reference
`POST /v1/jobs` examples did not provide a ready-to-run request for the policy.
Callers had to construct the nested options manually.

## User-facing change

- The API Reference `POST /v1/jobs` **Try it** selector now includes a
  `core_nt` sequence-diversity example.
- The example requests `outfmt: "7 std sseq"`, sets
  `result_selection_policy: "sequence_diversity"`, and demonstrates the
  separate `candidate_pool_size` request bound.
- The Service Bus Playground keeps the same nested `blast_options` contract.
  Its blank pool uses the server default, invalid pools below
  `max_target_seqs` remain blocked, and explicit pools above 5,000 remain valid.
- Selecting a Playground preset now replaces a manually edited request draft
  with that preset's executable JSON, so the form and the body sent by
  **Validate** or **Send** cannot describe different requests.
- Playground **Validate** applies the same local Service Bus wire-size budget as
  **Send** while remaining data-plane-free. An oversized request now returns the
  same `request_too_large` response before either branch.
- API Reference request-example and JSON-editor controls have explicit
  accessible names. Candidate-pool validation errors are linked to their input.
- The XML-only `/api/v1/elastic-blast/submit` facade remains intentionally
  unchanged; sequence diversity uses the tabular `POST /v1/jobs` path.

## API and infrastructure summary

- Authentication, RBAC, response envelopes, and infrastructure are unchanged.
- The existing typed API continues to accept any positive integer
  `candidate_pool_size >= max_target_seqs`; no fixed server maximum was added.
- Candidate-pool validation rejects booleans, floats, non-positive numbers,
  non-finite numbers, strings, arrays, and objects with the typed
  `sequence_diversity_invalid_candidate_pool` error.
- Server-added tabular merge fields can no longer push `outfmt` past its
  512-character transport contract; the request fails before submission with
  `outfmt_too_long_after_enrichment`.

## Hardening review

Twenty-five focused rounds covered the direct sibling contract, Service Bus
translation and persistence, validation abuse, React state transitions,
request serialization, proxy method/path/body fidelity, accessibility,
security, compatibility, documentation, and test false positives.

The review found and repaired four reproducible Medium defects:

- preset switching could leave an older manually edited JSON body as the
  executable request;
- tabular enrichment could turn a valid 511-character input into an invalid
  548-character wire value without a second length check;
- Playground dry-run could accept a request that real Send rejected at the
  Service Bus wire-size boundary.
- the post-enrichment length error was collapsed into a generic 400 response at
  the Playground boundary instead of preserving its typed 422 error code.

Accessibility and test-strength findings were also repaired: controls now carry
stable accessible names and error descriptions, and the Chromium test asserts
the actual proxy request is exactly `POST /v1/jobs` with the selected body.

No reproducible Medium, High, or Critical finding remains. Low residuals are
intentional UX and operational properties: mode-specific form values are kept
when switching away and back, selecting a different API example explicitly
loads that example, and very large positive pools may be rejected later by the
runtime's measured disk admission rather than an arbitrary fixed API maximum.

## Validation

- `uv run pytest -q api/tests/test_settings_service_bus.py::test_send_dry_run_accepts_sequence_diversity_above_legacy_limit`
- `npm test -- --run src/pages/apiReference/spec.test.ts`
- Playwright Chromium scenario: `API Reference exposes the sequence-diversity submit preset`
- The browser assertion selects the new example and verifies
  `max_target_seqs=100`, `candidate_pool_size=2000`, `outfmt="7 std sseq"`, and
  `result_selection_policy="sequence_diversity"` in the request editor.
- API and Service Bus related suites pass with `240 passed`.
- The full backend suite passes with `5,868 passed, 4 skipped`; the complete
  unfiltered suite passes with `6,025 passed, 4 skipped`. The skips require
  external parity evidence directories.
- Frontend validation passes with `1,024` unit tests, production build, ESLint,
  and both API Reference and Service Bus Playground Chromium scenarios. The API
  Reference scenario verifies the actual proxy method, path, and JSON body.
- Ruff, the production mypy debt ratchet, the 242-operation OpenAPI contract,
  generated TypeScript drift, docs frontmatter, and strict MkDocs all pass.
