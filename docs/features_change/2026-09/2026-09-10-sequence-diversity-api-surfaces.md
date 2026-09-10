---
title: Expose sequence diversity in API tools
description: Add the sequence-diversity request options to the API Reference Try it examples and verify the Playground API boundary.
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
- The XML-only `/api/v1/elastic-blast/submit` facade remains intentionally
  unchanged; sequence diversity uses the tabular `POST /v1/jobs` path.

## API and infrastructure summary

- No route, response, authentication, RBAC, or infrastructure contract changed.
- The existing typed API continues to accept any positive integer
  `candidate_pool_size >= max_target_seqs`; no fixed server maximum was added.
- Only the curated API Reference request examples and their test fixtures
  changed.

## Validation

- `uv run pytest -q api/tests/test_settings_service_bus.py::test_send_dry_run_accepts_sequence_diversity_above_legacy_limit`
- `npm test -- --run src/pages/apiReference/spec.test.ts`
- Playwright Chromium scenario: `API Reference exposes the sequence-diversity submit preset`
- The browser assertion selects the new example and verifies
  `max_target_seqs=100`, `candidate_pool_size=2000`, `outfmt="7 std sseq"`, and
  `result_selection_policy="sequence_diversity"` in the request editor.
- API and Service Bus related suites pass with `226 passed`.
- The full backend suite passes with `5,854 passed, 4 skipped`; the skips require
  external parity evidence directories.
- Frontend validation passes with `1,024` unit tests, production build, ESLint,
  and both API Reference and Service Bus Playground Chromium scenarios.
