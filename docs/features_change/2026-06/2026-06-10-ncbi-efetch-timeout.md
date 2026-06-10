---
title: NCBI efetch timeout for large GenBank records
description: Give the byte-streaming NCBI efetch path (GenBank XML / FASTA) a longer timeout so large genome records load on the first attempt instead of timing out.
tags:
  - blast
  - architecture
---

# NCBI efetch timeout for large GenBank records

## Motivation

Opening the Sequence Detail page (`/sequence/:accession`) for a large NCBI
nucleotide record — e.g. `PQ221797.1`, a ~197 kb Monkeypox virus genome — left
"Loading features…" on screen for ~26 s and then failed with a misleading
`GenBank record lookup failed` 503.

Root cause: the GenBank **efetch** XML embeds the full sequence plus every CDS
`translation`, so the body is large (~640 KB for `PQ221797.1`) and NCBI
generates it slowly server-side (measured 9.5–16.7 s). The client timeout was
fixed at 8 s (`DEFAULT_TIMEOUT_SECONDS`), so every attempt timed out, was
classified transient, and was retried twice (`8 + 0.5 + 8 + 1.5 + 8 ≈ 26 s`)
before a 503 the record could never have satisfied.

## User-facing change

Large GenBank records now load on the first attempt (~10–16 s) instead of
failing after ~26 s. Small records and typo accessions are unaffected — the
cheap esummary header call keeps its fast 8 s budget so an invalid accession
still fails quickly.

## API / IaC diff summary

* `api/services/ncbi/_eutils.py`:
  * Added `_DEFAULT_EFETCH_TIMEOUT_SECONDS = 30.0` and
    `_efetch_timeout_seconds()` — read at call time, overridable via
    `NCBI_EFETCH_HTTP_TIMEOUT`, floored at `DEFAULT_TIMEOUT_SECONDS` so the
    efetch path can never be made faster-failing than esummary.
  * `request_bytes(...)` now passes that timeout to `client.stream(..., timeout=...)`
    per request; the small esummary JSON path (`request_json`) is unchanged.
* No IaC change. No frontend change.

## Validation evidence

* Measured live latency for `PQ221797.1` efetch: 9.5–16.7 s at 640 KB
  (`curl ... efetch.fcgi?db=nuccore&id=PQ221797.1&rettype=gb&retmode=xml`).
* `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 58 passed (3 new:
  `test_efetch_timeout_default_exceeds_summary_timeout`,
  `test_efetch_timeout_env_override_and_floor`,
  `test_request_bytes_passes_efetch_timeout_to_stream`).
* `uv run ruff check api/services/ncbi/_eutils.py api/tests/test_ncbi_nuccore.py` → clean.

## Follow-up

First-view latency for large records is still slow. Caching / lighter efetch /
async load tracked in issue #27.
