---
title: Strict same-snapshot Web BLAST parity gate
description: Separate cross-snapshot diagnostics from exact equality and compare every canonical HSP, parameter, and search-statistics field before accepting live parity evidence.
tags:
  - blast
  - research
---

# Strict same-snapshot Web BLAST parity gate

## Motivation

The [NCBI Web BLAST](https://blast.ncbi.nlm.nih.gov/) candidate comparator said
that database drift downgraded validation to accession-set diagnostics, but it
still appended every HSP drift to the active failure list. More importantly, the
issue-closing test asserted the mode-dependent `equivalent` field rather than a
strict same-snapshot verdict. The parser retained only the first HSP per subject
and did not compare raw score, gaps, coordinates, frames, aligned sequences, or
complete search statistics required by issue #8.

This combination was both internally inconsistent and unsafe for a complete
parity claim: drift mode could never behave as documented, while a future relaxed
pass could be mistaken for exact equality.

## User-facing change

- `ParityReport.exact_equivalent` is now the only complete-parity verdict.
- `ParityReport.equivalent` remains the selected comparison-mode result for
  backward compatibility; `comparison_mode` makes its meaning explicit.
- Cross-snapshot validation reports `drift_compatible` containment diagnostics
  without claiming exact equality. An empty candidate never passes containment.
- Supplying `ELB_PARITY_CANDIDATE_DIR` now requires `exact_equivalent=true` for
  every F3L, 18S, and RdRp/ORF1ab candidate. Once the directory is supplied, a
  missing candidate fails instead of being skipped.
- Failure output distinguishes active diagnostic findings from strict
  `exact_findings` and includes structured subject/HSP differences.
- Taxonomy fixture labels were corrected from over-broad genus-like names to
  `Orthopoxvirus monkeypox` and `Betacoronavirus pandemicum`. Query-source
  accession exclusion remains green. An authoritative lookup found the ORF1ab
  rank-3 subject `MN996528.1` at descendant taxid `2697049`; because that XML
  hit groups excluded and non-excluded identical-sequence deflines without
  per-defline taxids, the fixture now records an explicit AC6 blocker instead
  of overclaiming organism exclusion.
- Legacy Web XML v1 references expose 1.2–1.5 billion `db-len` and zero
  `eff-space`, insufficient to identify the full trillion-base `core_nt`
  snapshot. Exact closure now also requires authoritative reference release
  counts outside that wrapped/filtered XML representation.
- Pre-flight now calls the same canonical `submit_contracts()` resolver as the
  real submit path, so live database sequence counts, recalculated search space,
  precise-mode oracle enablement, and future option fields cannot drift between
  the preview and execution decisions.
- External/OpenAPI XML submissions now carry `soft_masking=false` alongside
  `dust=true`, matching the dashboard's `FILTER=L` configuration instead of
  silently relying on the BLAST+ soft-masking default.
- Deployed precise `core_nt` submits now read active database letters/sequences,
  replace stale caller `-searchsp` or `-dbsize` values with one calculated
  `-searchsp`, and fail closed if active metadata is unavailable. Dashboard
  external, canonical inline, Service Bus XML/tabular, and sibling `/v1/jobs`
  use the same formula.
- API Reference and Service Bus Playground presets no longer embed the obsolete
  2026-05 search-space constant; they leave this server-owned value unset.

## API and implementation summary

- `WebBlastSummary` now retains the complete BLAST parameter block and database,
  HSP-length, effective-space, kappa, lambda, and entropy statistics.
- `WebBlastHit` retains subject rank, identifiers, definition, length, and every
  `WebBlastHsp`, while preserving its former first-HSP compatibility fields.
- Strict comparison covers raw/bit score, e-value, identity, positives, gaps,
  alignment length, query/subject coordinates, frames, qseq, hseq, midline,
  HSP count, subject order, BLAST version, and parameters.
- The dashboard parser guard compares all 2,653 captured HSP rows across 23
  projected UI/API/export fields. Canonical subject IDs are extracted from
  versioned `Hit_id` values while raw `Hit_accession` remains independently
  checked, handling NCBI multi-defline hits without field substitution.
- Multi-query XML is rejected by this single-query reference harness instead of
  silently comparing only the first iteration. Duplicate normalized accessions
  are reported as ambiguous rather than collapsed into a false pass.
- No IaC, dependency, Storage network, or browser-download change is involved.
- The sibling OpenAPI build-context patch extends both its request schema and
  internal option bridge; patching stays idempotent and fails if either marker
  is missing or duplicated.
- Runtime ACR rebuilds now source the dashboard repository, fetch and verify the
  pinned sibling commit, apply the checked-in OpenAPI patcher, and build the
  generated context. The former raw-sibling rebuild path could silently drop
  every dashboard overlay.
- Runtime rebuilds lease public ACR build access and require verified private
  restoration before the new OpenAPI image may deploy; the image build timeout
  is bounded below the orchestrator deadline.
- Immutable image `elb-openapi:4.38` was built from sibling commit `352a1f4`
  plus the reviewed dashboard overlay in ACR run `de7s`; digest
  `sha256:73073111e32ae7fc03b4d2f23e6ecbe7ef0247ade0a67b52b4087b412e01f85c`.
  It pins full-DB/shard URLs, search space, oracle generation, and reported DB
  provenance to one validated active generation. Intermediates `4.36`/`4.37`
  were never deployed and were deleted after `4.38` succeeded. Deployment
  remains deferred while AKS is intentionally Stopped.
- The only DB representation normalization strips URL/path and shard suffixes
  to the logical database leaf. Query deflines remain strict; generated
  `query_id` values are intentionally ignored because Web BLAST and local
  BLAST+ assign different transport identifiers to the same FASTA record.

## Validation

- `uv run pytest -q -W error::DeprecationWarning api/tests/test_web_blast_parity_xml.py`
  covers strict self-equivalence, cross-snapshot diagnostics, empty-candidate
  rejection, every canonical HSP field, all-HSP retention, subject/HSP counts,
  parameters, and search statistics.
- Full backend: `5,750 passed, 4 skipped` with slow/subprocess tests included.
  Three skips are the explicitly blocked live F3L/18S/ORF1ab candidates; the
  sibling enum sync skip was separately exercised against a fresh clone (`2
  passed`).
- Frontend: `110` files / `994` tests passed, followed by zero-warning ESLint
  and a successful production build.
- Ruff, Python compile, Bash syntax, ShellCheck, `git diff --check`, docs
  frontmatter, and MkDocs strict build passed.
- Native BLAST+ 2.17.0 generated full DB versus three contiguous shards:
  oracle concatenation exact, tabular exact, XML `difference_count=0`, and
  `xml_statistics=full_db_exact`.
- Fresh sibling patching passed generated-app AST policy, Python compile, and
  second-application byte identity. The ACR pre-build command independently
  fetched and verified sibling `352a1f4`, patched it, and compiled the result.
- Container App tag `deploy-26218362` is live in revision
  `ca-elb-dashboard--env-terminal-1788366634-13727`: six containers Ready,
  restart count zero, health/terminal health 200. Post-deploy App Insights had
  zero failed requests, exceptions, severe traces, and failed dependencies.
  This later hardening commit is not yet deployed.
- Live acceptance candidates were not started: the existing ten-node AKS stays
  `Stopped`, and frozen reference snapshot/taxonomy prerequisites cannot pass
  the exact gate. Issue #8 therefore remains open.
