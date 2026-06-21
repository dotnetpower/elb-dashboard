---
title: BLAST results show as "degraded" for large successful searches — parse oversized result XML
description: Oversized BLAST result XML truncated at the analytics byte cap raised ParseError and showed a false "results degraded / RBAC-network outage" banner with zero hits; the parser now returns the hits before the cut and the UI marks the result partial.
tags:
  - operate
  - blast
  - ui
---

# BLAST results show "degraded" for large successful searches

## Motivation
On the live dashboard a completed `core_nt` BLAST job (search id `c8492da4…`,
query `orf1b|43740578`, 21 290 bp) rendered **"Results are degraded — Every
result file failed to download. RBAC, network outage, or the storage account is
unreachable. Successfully parsed 0 of 1 result file. 1 read failure."** with
`0 hits`, even though the search had actually **succeeded with 503 hits**.

## Root cause (confirmed with the real artifact, not guessed)
* The result blob `…/merged_results.out.gz` is a **single, complete, well-formed
  BLAST XML** document — **32 MB decompressed** (one `<?xml>`, one `<BlastOutput>…
  </BlastOutput>`). Storage was reachable: the blob *list* and *download* both
  succeeded, so it was never an RBAC / network / storage issue.
* The analytics read paths cap the decompressed payload:
  `RESULTS_AGGREGATE_MAX_BYTES = 10 MB` (Descriptions stats) and
  `RESULTS_ALIGNMENTS_MAX_BYTES = 20 MB` (hit table). The 32 MB result is cut at
  the cap, producing a **mid-element-truncated XML string**.
* `parse_blast_xml` streams the document with `ElementTree.iterparse`. At the
  truncated EOF it raised `xml.etree.ElementTree.ParseError`, which propagated
  out and **discarded every hit parsed so far** → counted as a read failure →
  `all_reads_failed` → the alarming red "degraded" banner with zero hits.
* Reproduced locally with the real file: full 32 MB → 503 hits; truncated to
  20 MB → `ParseError`; truncated to 10 MB → `ParseError`. Scope check: only this
  one oversized job hit the parse failure in 48 h (not systemic).

## Fix
* **`api/services/blast/results_parser.py`** — `parse_blast_xml` now catches a
  `ParseError` from the streaming loop and **returns the complete `<Hit>` rows
  collected before the cut** instead of discarding the whole result. If *no*
  hit parsed (genuinely corrupt file) it **re-raises**, so a real read failure
  is still recorded. Verified on the real artifact: 20 MB cut → 324 hits,
  10 MB cut → 161 hits, garbage → still raises.
* **`api/services/blast/result_artifacts.py`** — both read paths (`_read_hits`
  and `build_result_aggregate_payload`) now flag the result **`truncated`** when
  a read fills the byte budget, so the UI honestly shows "Results are partial"
  instead of presenting a clipped result as complete.
* **`web/src/pages/blastResults/analytics/DegradedBanner.tsx`** — corrected the
  misleading `all_reads_failed` copy. Since the blob list already succeeded,
  this state is never an RBAC / network / storage problem; it now reads "The
  result files were downloaded but could not be parsed — the output may be
  corrupted or in an unexpected format."

## User-facing change
A large successful search now shows its hits (marked "partial" when the result
exceeds the analytics byte cap) instead of a false "results degraded" banner
with zero hits. No security configuration changed (no auth / RBAC / network /
JWT / CORS edits); the fix is parser resilience + honest UI labelling.

## Follow-up (not in this change)
For full-fidelity display of very large results, raise/unify the analytics byte
caps or paginate the parse so the hit table and aggregate stats cover the entire
file rather than the first 10–20 MB. Bounded by api-sidecar memory under the
20-file (`RESULTS_MAX_FILES`) worst case, so it needs a per-job total-bytes
budget rather than a blanket cap bump.

## Validation
* Backend: `uv run ruff check api` clean; `uv run pytest -q api/tests` → 4136 passed
  (incl. new `test_parse_xml_truncated_at_byte_cap_returns_partial_hits` and
  `test_parse_xml_unparseable_from_start_still_raises`).
* Frontend: `npx vitest run` → 924 passed; `npm run build` clean.
* Real-data repro + fix verified against the live 32 MB artifact (fetched via the
  cluster, not committed): full 503 hits, 20 MB-truncated 324 hits.
