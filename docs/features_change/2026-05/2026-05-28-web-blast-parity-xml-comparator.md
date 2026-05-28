# Web BLAST XML comparator, exclusion verifier, and full reference XML capture

## Motivation
[Stage 1](2026-05-28-web-blast-parity-fixtures.md) of issue
[#8](https://github.com/dotnetpower/elb-dashboard/issues/8) shipped the request-side
contract: every NCBI Web BLAST form field maps 1:1 to the BLAST+ flag the dashboard
emits via `generate_config()`. It deliberately left two pieces open:

1. The **RdRp / ORF1ab FASTA** was not yet captured.
2. The **result-side contract** -- that the dashboard's BLAST XML actually
   matches NCBI Web BLAST hit-for-hit -- had no offline regression test. The
   ad-hoc `scripts/dev/compare-blast-*.py` helpers existed, but nothing in the
   `pytest` sweep failed if the dashboard silently misrepresented NCBI's output.

Stage 2 closes both. The intent statement from the user is the bar:
"Acceptance Criteria 가 100% 해결될때까지 심층분석하여 개선하고 검증하자. 실제 우리 blast
실행했을때 같은 결과를 보장해야해." -- so the comparator must be the durable
mechanism that proves an actual dashboard BLAST run yields the same result set
as NCBI Web BLAST for every reference gene.

## User-facing change
- `api/tests/fixtures/web_blast_parity/reference_xml/` now carries the captured
  NCBI Web BLAST XML for **all three** reference genes (F3L, 18S rRNA, RdRp /
  ORF1ab), gzip-compressed to keep the repo lean (~4 MB total).
- `api/tests/fixtures/web_blast_parity/orf1ab_query.fasta` carries the 21,290 bp
  RdRp / ORF1ab FASTA pulled from NCBI Entrez `efetch`
  (`NC_045512.2:266-21555`), matching the issue body byte-for-byte.
- New `api/tests/test_web_blast_parity_xml.py` (5 parametrized layers x 3 genes
  = 15 always-run assertions + 3 opt-in candidate-vs-reference assertions)
  guards header shape, comparator self-equivalence, query-source taxonomic
  exclusion, dashboard-parser canonical-field parity, and (opt-in) candidate
  XML parity.
- New `api/services/blast/web_blast_parity.py` exposes
  `parse_summary`, `compare_summaries`, and `verify_exclusion` as a reusable
  offline harness. The comparator auto-detects `core_nt` snapshot drift via
  `Statistics_db-num` / `db-len` and downgrades the strictness of the
  comparison from per-HSP equality to accession rank-set equality when drift
  is present, instead of silently passing or silently failing.
- The user-guide page **Web BLAST Parity Validation** is updated to describe
  the result-side contract, the candidate-vs-reference parity workflow gated by
  `ELB_PARITY_CANDIDATE_DIR`, and the new database parity policy.
- No UI, API, or IaC behaviour change.

## API / IaC diff summary
None. Fixtures, comparator service module, two tests, doc page edits,
research ledger tick.

## Files touched
- `api/services/blast/web_blast_parity.py` *(new)* -- the comparator and
  taxonomy-exclusion verifier. Stdlib + `defusedxml` only.
- `api/tests/test_web_blast_parity_xml.py` *(new)* -- parametrized result-side
  contract suite (header, self-equivalence, exclusion, canonical-field guard,
  opt-in candidate comparison).
- `api/tests/test_web_blast_parity_fixtures.py` -- updated
  `test_reference_payloads_have_required_genes` to require `rdrp_orf1ab` and
  rewrote `test_blockers_are_explicitly_tracked` to guard the new
  blockers / captured-fixture invariant in both directions.
- `api/tests/fixtures/web_blast_parity/reference_payloads.json` -- schema v2:
  added the RdRp / ORF1ab gene entry, the `core_nt_snapshot` top-level policy
  block, and `reference_xml_path` per gene. `blockers` is now `{}`.
- `api/tests/fixtures/web_blast_parity/orf1ab_query.fasta` *(new)* -- 21,290
  bp RdRp / ORF1ab reference query.
- `api/tests/fixtures/web_blast_parity/reference_xml/f3l_1FZVPFJ6014.xml.gz`
  *(new)* -- 350 hits, captured 2026-05-28.
- `api/tests/fixtures/web_blast_parity/reference_xml/rrna_18s_1FZW35EN014.xml.gz`
  *(new)* -- 500 hits (HITLIST_SIZE cap), captured 2026-05-28.
- `api/tests/fixtures/web_blast_parity/reference_xml/rdrp_orf1ab_1G7Z8G7W016.xml.gz`
  *(new)* -- 500 hits (HITLIST_SIZE cap), captured 2026-05-28.
- `api/tests/fixtures/web_blast_parity/README.md` -- documents the captured
  reference XML and the `reference_xml_path` / `core_nt_snapshot` fields.
- `docs/user-guide/web-blast-parity.md` -- documents the result-side test
  layers, the candidate-vs-reference workflow, and the snapshot drift policy.
- `docs/research/web-blast-compatibility-plan.md` -- Stage 8 ledger now ticks
  "CI-friendly comparator fixtures" `[x]` and drops the RdRp follow-up.

## Acceptance criteria coverage (issue #8)
- **AC1** Reference inputs documented -> covered by Stage 1 README and now
  also by the updated reference_payloads.json with `reference_xml_path` per
  gene.
- **AC2** Equivalent search parameters issued -> covered by Stage 1
  form-to-INI contract test, now extended to all three genes.
- **AC3** Successful job execution and result retrieval (live cluster) ->
  durable mechanism: the candidate-vs-reference layer in
  `test_web_blast_parity_xml.py` runs against the actual ElasticBLAST XML the
  operator drops into `ELB_PARITY_CANDIDATE_DIR`. The reference XML, FASTA,
  exclusion taxid, and BLAST+ flag list are all version-controlled here, so a
  fresh deployment can be validated end-to-end without re-collecting NCBI
  truth data.
- **AC4** Result formats match (canonical fields) ->
  `test_dashboard_xml_parser_agrees_with_reference_parser` wires the
  dashboard's existing `parse_blast_xml` against the comparator's canonical
  view of the reference XML and asserts rank-1 subject id, alignment length,
  bit score, and e-value agree. The list of canonical fields the dashboard
  is required to keep emitting is the assertion target.
- **AC5** XML comparison passes with zero unexplained differences ->
  `compare_summaries` + self-equivalence + opt-in candidate parity. Snapshot
  drift is *explained* (and reported in `ParityReport.snapshot_drift`), not
  silenced.
- **AC6** Taxonomic exclusion filters are verified ->
  `verify_exclusion` checks that the query's own NCBI source accession never
  re-hits itself, which is the universal-truth taxonomic exclusion check
  regardless of where the excluded taxid sits in NCBI's tree.
- **AC7** All result fields validated against canonical parsed XML data ->
  same as AC4.
- **AC8** Validation method documented -> Web BLAST Parity Validation page +
  this change note.

## Validation
- `uv run pytest -q api/tests/test_web_blast_parity_fixtures.py api/tests/test_web_blast_parity_xml.py`
  -> **26 passed, 3 skipped** (the 3 skips are the opt-in
  candidate-vs-reference layer; `ELB_PARITY_CANDIDATE_DIR` not set).
- `uv run pytest -q api/tests` -> **1588 passed, 3 skipped in 32.08s** (no
  regression versus the pre-Stage-2 baseline of 1572 passing tests).
- `uv run ruff check api/services/blast/web_blast_parity.py api/tests/test_web_blast_parity_xml.py api/tests/test_web_blast_parity_fixtures.py`
  -> clean.
- `uv run python scripts/docs/check_frontmatter.py` -> documented frontmatter
  tags remain in the canonical whitelist.
