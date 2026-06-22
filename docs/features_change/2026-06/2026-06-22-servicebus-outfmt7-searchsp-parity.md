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
the dashboard oracle so dashboard BLAST results match NCBI Web BLAST e-values.
The dashboard **New Search** native path applies it correctly
(`api/services/blast/config.py` `generate_config` → `-searchsp <N>`). Tracing the
Service Bus request-queue path surfaced that the external submit surfaces do
**not** apply the dashboard-computed value — they rely on the sibling's fixed
default instead:

- **Sibling auto-inject (verified):** the sibling `submit_job`
  (`docker-openapi/app/main.py`) appends a **fixed** `-searchsp 32156241807668`
  whenever the BLAST options carry no `-searchsp` / `-dbsize`. That value is
  core_nt's calibration, so it is correct **only** for core_nt.
- **XML path** (`options` → `/api/v1/elastic-blast/submit`): the sibling
  `external_submit` handler **drops** the dashboard's `db_effective_search_space`
  and builds its own `extra` (word_size / dust only), then delegates to the same
  `submit_job` → it too falls back to the fixed default. So the dashboard's
  per-database oracle value never reaches BLAST on this path.
- **Free-form path** (`blast_options` → sibling `/v1/jobs`, the **only** way to
  request a multi-token `outfmt 7`): `BlastV1Options` had no structured searchsp
  field and `_build_v1_jobs_payload` did not resolve one → same fixed default.

For core_nt all three coincide (the fixed default equals the calibration), so the
gap is invisible until a caller-supplied value, a snapshot drift, or a future
per-database calibration is involved.

## User-facing change

An outfmt-7 Service Bus submit now forwards the dashboard's resolved
`searchsp` to BLAST, so it matches the value the **New Search** native path
emits (rather than always relying on the sibling's fixed default):

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
