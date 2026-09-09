---
title: OpenAPI reference context resolver
description: Add an authenticated, bounded RID evidence resolver, explicit result-selection controls, and typed readiness responses.
tags:
  - blast
  - user-guide
  - security
---

# OpenAPI reference context resolver

## Motivation

API callers could submit a measured six-field Web BLAST statistical context, but the OpenAPI
service did not provide a bounded way to derive it from an already completed reference result.
The API schema also omitted readiness fields that the runtime already returned, while the Service
Bus playground did not expose the native-versus-diversity result-selection choice.

## User-facing change

- Authenticated callers can use `POST /v1/web-blast/statistical-context` with one completed
  [NCBI BLAST](https://blast.ncbi.nlm.nih.gov/Blast.cgi) RID and its exact single-record FASTA.
- The response returns the measured six-field context, per-query effective search space, evidence
  hashes, active-generation metadata, and explicit flags for provenance that the result formats
  cannot verify.
- Pending evidence returns `409` with `Retry-After: 30`; inconsistent evidence returns `422`; an
  unavailable external result service, active database metadata path, or busy resolver gate returns
  a retryable `503`.
- OpenAPI job status and list operations publish additive typed response models containing
  `results_ready`, `results_ready_at`, `merged_at`, `db_partitions`, and
  `result_selection_policy`.
- API Reference examples now state `result_selection_policy: native_top_n` explicitly. The Service
  Bus playground exposes the same control and includes an opt-in `diversity_aware` preset whose
  text makes the heuristic membership tradeoff explicit.

## API and runtime summary

- The build-context patch copies a standalone resolver overlay into the sibling OpenAPI app and
  pins `defusedxml==0.7.1` in the image requirements.
- External XML is fetched only from the fixed NCBI result endpoint with redirects disabled,
  connect/read timeouts, a 128 MiB compressed and expanded limit, bounded ZIP members, hardened XML
  parsing, and process-wide request pacing.
- Cache entries are bounded, keyed by RID, query digest, active generation, counts, and filter
  expectation, and deep-copied on both write and read. Cache misses use a single fetch gate with a
  two-second acquisition limit so concurrent unique requests cannot queue indefinitely.
- The `/v1` router-level API-token dependency protects the new route. Storage OAuth tokens and the
  optional NCBI API key never appear in responses. No SAS URL, Storage public-access path, RBAC
  assignment, or infrastructure topology changes.
- The resolver validates query length, database identity/counts, length encoding, and the integer
  formulas. It deliberately reports that original query content, taxonomy expression, all submit
  options, and source-version identity are not provable from the result formats alone.

## Validation

- `uv run pytest -q api/tests/test_openapi_reference_context.py api/tests/test_patch_openapi_build_context.py`: 51 passed.
- `uv run pytest -q api/tests`: 5,807 passed and 5 environment-dependent evidence checks skipped.
- `npm --prefix web test -- --run`: 1,023 passed across 116 files. ESLint and the Vite
  production build passed.
- The patcher was reapplied to a previously patched sibling context. The refreshed app contained
  the pinned hardened parser and upgraded route boundary.
- A network-free FastAPI TestClient smoke against that generated sibling app returned `401` without
  a token, `200` with a token and typed response filtering, and a sanitized `503` for active
  metadata failure.
- The complete generated sibling suite passed 113 tests in an isolated Python 3.11 environment
  using the app's declared runtime and test requirements.
- Full Ruff, the production mypy debt ratchet, the 242-operation OpenAPI contract check, generated
  TypeScript drift check, docs frontmatter guard, and strict MkDocs build passed.
- Playwright verified the opt-in selection control and generated request body at 1440 x 1000 and
  390 x 844. Both viewports had no horizontal overflow; the mobile control remained within the
  viewport at approximately 309 px wide.
