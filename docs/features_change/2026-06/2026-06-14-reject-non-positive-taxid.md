---
title: Reject non-positive taxid at the external BLAST submit boundary
description: taxid must be a positive NCBI taxonomy id; 0 / negative values are now rejected with 422 instead of being forwarded as a nonsensical organism filter.
tags:
  - blast
  - api
---

# 2026-06-14 — Reject non-positive `taxid` at submit

## Motivation

During a live diverse-scenario validation pass, an invalid-payload probe found
that `POST /api/v1/elastic-blast/submit` accepted a **negative** `taxid` (`-5`)
with HTTP 202. NCBI taxonomy ids are positive integers (the tax tree root is 1),
so `0` / negative is never a valid organism filter — forwarding it to the sibling
would either error mid-run or silently filter out every hit. This was the only
gap in an otherwise-clean hardening sweep (non-FASTA query, invalid program,
non-XML outfmt, path-injection db, `is_inclusive` without `taxid`, empty FASTA
were all already rejected 4xx).

## User-facing change

* `taxid` on the external BLAST submit model now requires `1 ≤ taxid ≤ 2147483647`.
  A `0` / negative / out-of-range value is rejected with **HTTP 422** at the
  boundary instead of being accepted. This applies to every path that uses the
  shared `ExternalBlastSubmitRequest` model: `POST /api/v1/elastic-blast/submit`,
  the inline-FASTA `POST /api/blast/jobs`, and the Service Bus request bridge.
* A valid positive `taxid` (e.g. `562`) is unchanged.

## API / IaC diff summary

No API surface change. Internal only:

* `api/routes/elastic_blast.py` — `ExternalBlastSubmitRequest.taxid` gains
  `Field(None, ge=1, le=2_147_483_647)`.

## Validation evidence

* Live probe before fix: `{"taxid": -5}` → 202 (accepted). After fix: 422.
* `uv run pytest -q api/tests/test_external_blast_api.py` → **87 passed** (incl.
  new `test_external_blast_rejects_non_positive_taxid`).
* `uv run ruff check api` → clean.
* Broader live validation that surfaced this: OpenAPI Mode B / B+taxid / option
  variants all accepted + ran; idempotency replay (same key → same job_id);
  Service Bus fan-in with a duplicate correlation created only one bridge
  (dedup); dashboard inline-FASTA submit delegated to OpenAPI (202,
  `job_id_kind=openapi`); App Insights showed **0 5xx** across the whole sweep.
