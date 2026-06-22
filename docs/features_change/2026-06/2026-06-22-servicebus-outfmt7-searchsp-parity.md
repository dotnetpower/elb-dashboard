---
title: Service Bus outfmt-7 path applies the Web BLAST searchsp oracle
description: The free-form /v1/jobs (blast_options / outfmt 7) Service Bus submit path now resolves and forwards the same calibrated -searchsp the XML path and New Search apply, so e-value parity with NCBI Web BLAST no longer depends on the sibling's fixed default.
tags:
  - blast
  - architecture
---

# Service Bus outfmt-7 path applies the Web BLAST searchsp oracle

## Motivation

A researcher calibrates the Web BLAST effective search space (`searchsp`) with
the dashboard oracle so dashboard BLAST results match NCBI Web BLAST e-values,
and applies it on the API submit. Tracing the Service Bus request-queue path
surfaced an asymmetry:

- **XML path** (`options` → `ExternalBlastSubmitRequest` →
  `/api/v1/elastic-blast/submit`): the drain runs the shared
  `resolve_sharding_plan`, which resolves the calibrated / drift-adjusted /
  caller-supplied `searchsp` and forwards it. **Parity applied.**
- **Free-form path** (`blast_options` → `ExternalBlastV1Request` →
  sibling `/v1/jobs`, the **only** way to request a multi-token `outfmt 7`):
  `BlastV1Options` had **no structured searchsp field** and
  `_build_v1_jobs_payload` did **not** run `resolve_sharding_plan`. The sibling
  `/v1/jobs` then auto-injects a **fixed default** `-searchsp 32156241807668`
  when none is present — correct only for the database it was calibrated against
  (core_nt), and never reflecting a caller-supplied value, a snapshot drift, or
  a future calibrated database. **Parity NOT applied on outfmt 7.**

## User-facing change

An outfmt-7 Service Bus submit now gets the **same** calibrated `-searchsp` as
the XML path and the dashboard New Search:

- `BlastV1Options` gained an optional `db_effective_search_space` field
  (mirrors the XML path's `ExternalBlastOptions.db_effective_search_space`).
- `_build_v1_jobs_payload` resolves the search space through the shared
  `resolve_sharding_plan(..., allow_servicebus_downgrade=True)` (never blocks —
  it degrades) and forwards the result as a raw `-searchsp <N>` flag in
  `blast_options.extra`.
- A caller-pinned `-searchsp` / `-dbsize` (in `extra`/`outfmt`) is **never**
  overridden. The `db_effective_search_space` convenience field is **not** a
  sibling wire field, so it is stripped before the payload leaves.
- searchsp resolution is wrapped so it can **never** fail a valid submit — on
  any error it skips injection and the sibling applies its own default.

**No regression for the common case:** core_nt's calibrated value
(`32156241807668`) equals the sibling's fixed default, so a steady-state core_nt
outfmt-7 submit yields the identical `-searchsp`. The change only differs from
the prior behaviour where the prior behaviour was already wrong (caller-supplied
value, snapshot drift, or a future per-database calibration).

## Files

- `api/routes/elastic_blast.py` — `BlastV1Options.db_effective_search_space`.
- `api/tasks/servicebus/tasks.py` — `_build_v1_jobs_payload` searchsp resolution +
  `-searchsp` injection (strip the non-wire field, honour caller-pinned values,
  failure-safe).

## Design critique (self-critique rubric)

- **Contract:** additive optional field; stripped before wire; the sibling
  ignores unknown fields anyway. Consumers (`_validate_send_body`, drain,
  sibling) unaffected.
- **Idempotency:** pure transform on a fresh `model_dump` dict; the
  `-searchsp`/`-dbsize` guard prevents double-injection.
- **Partial failure:** `resolve_sharding_plan` is wrapped in try/except — a
  resolution error never dead-letters a valid job.
- **Liveness:** no loops. **Security:** searchsp is an int; no new boundary.
- **Backward-compat:** core_nt steady-state value unchanged; field is optional.

## Validation

- `uv run pytest -q api/tests/test_servicebus_v1_multitoken.py` — 14 passed
  (3 new: oracle injection when absent, structured field honoured + stripped,
  caller-pinned `-searchsp` not overridden).
- `uv run pytest -q api/tests/test_servicebus_tasks.py
  api/tests/test_servicebus_v1_multitoken.py api/tests/test_servicebus_load.py`
  — 61 passed.
- `uv run ruff check api/tasks/servicebus/tasks.py api/routes/elastic_blast.py`
  — clean.

## Not in scope

Suppressing the sibling's fixed default `-searchsp` for an **uncalibrated**
database (the dashboard has no oracle value to substitute, so it leaves the
sibling default as-is). Only core_nt is calibrated today.
