---
title: Web BLAST Parity Validation
description: How to verify that BLAST jobs run from this dashboard produce equivalent results to NCBI Web BLAST for the captured reference diagnostic genes (F3L, 18S rRNA, RdRp / ORF1ab).
tags:
  - user-guide
  - blast
  - research
---

# Web BLAST Parity Validation

This page describes the durable validation path that proves this dashboard's BLAST execution and
results are equivalent to [NCBI Web BLAST](https://blast.ncbi.nlm.nih.gov/) for the captured
reference diagnostic genes. The full ledger of how this is implemented end to end (compatibility
contract, evidence registry, sharding precision) lives in
[Web BLAST Compatibility Plan](../research/web-blast-compatibility-plan.md); this page is the
short, practical walkthrough.

Tracking issue: [#8 Validate BLAST result parity with NCBI Web BLAST references](https://github.com/dotnetpower/elb-dashboard/issues/8).

## Reference genes

| Gene | Pathogen | Query length | NCBI RID (captured) | Entrez exclusion | Reference XML |
| --- | --- | --- | --- | --- | --- |
| F3L | Monkeypox virus (`taxid=10244`) | 462 bp | `9MHUJ94R014` | `NOT txid3431483[ORGN]` (`Orthopoxvirus monkeypox`, species) | `reference_xml/f3l_9MHUJ94R014.xml.gz` |
| 18S ribosomal RNA | Plasmodium falciparum (`taxid=5833`) | 2,151 bp | `9N5JA17Y014` | `NOT txid5833[ORGN]` (P. falciparum itself) | `reference_xml/rrna_18s_9N5JA17Y014.xml.gz` |
| RdRp / ORF1ab | SARS-CoV-2 (`taxid=2697049`) | 21,290 bp | `9MK93UBF016` | `NOT txid3418604[ORGN] NOT txid32630[ORGN]` | `reference_xml/rdrp_orf1ab_9MK93UBF016.xml.gz` |

All three FASTA inputs and their corresponding NCBI Web BLAST reference XML outputs are checked
into the repository under `api/tests/fixtures/web_blast_parity/`. The reference XMLs are stored
gzip-compressed (`.xml.gz`) to keep the repo lean; the comparator reads `.xml` and `.xml.gz`
transparently. The RdRp / ORF1ab FASTA was captured from NCBI Entrez `efetch` against
`NC_045512.2:266-21555` and matches the issue body byte-for-byte.

## What the parity tests actually check

The parity validation is now split across two complementary test files. Both are offline,
deterministic, and run as part of the default `uv run pytest -q api/tests` sweep.

### Request-side contract (form -> INI -> BLAST+ flags)

`api/tests/test_web_blast_parity_fixtures.py` asserts the **request-side** contract: every NCBI
Web BLAST form parameter maps 1:1 into a BLAST+ flag that the dashboard's
[`generate_config()`](../../api/services/blast/config.py) builder emits in the elastic-blast INI.
That is what guarantees the same inputs are sent to BLAST+ in both environments.

| NCBI Web BLAST form | Dashboard submit field | BLAST+ flag emitted |
| --- | --- | --- |
| `PROGRAM=blastn` | `program=blastn` | `[blast].program=blastn` |
| `DATABASE=core_nt` | `database_name=core_nt` | `db=blast-db/core_nt/core_nt` |
| `FORMAT_TYPE=XML` | n/a (transport-only) | n/a -- comparison harness reads XML directly. |
| `HITLIST_SIZE=500` | `max_target_seqs=500` | `-max_target_seqs 500` |
| `EXPECT=0.05` | `evalue=0.05` | `-evalue 0.05` |
| `MEGABLAST=on` | `program=blastn` (implicit task) | (none -- modern BLAST+ defaults `blastn` to `-task megablast` when `WORD_SIZE` is megablast-typical). |
| `WORD_SIZE=28` | `word_size=28` | `-word_size 28` |
| `FILTER=L` | `low_complexity_filter=true` | `-dust yes -soft_masking false` |
| `ENTREZ_QUERY=NOT txid<N>[ORGN]` | `taxid=N, is_inclusive=false` | `-negative_taxids <N>` |

### Result-side contract (canonical XML view + exclusion + candidate parity)

`api/tests/test_web_blast_parity_xml.py` asserts the **result-side** contract against the
captured NCBI Web BLAST XML for every reference gene:

1. **Header guard.** The XML must declare `blastn` + BLASTN 2.x + `core_nt` + a matching
   `query_len`, `EXPECT`, and `FILTER` -- otherwise the captured XML belongs to a different
   query and parity claims are meaningless.
2. **Self-equivalence.** `compare_summaries(reference, reference)` must return
  `exact_equivalent=True` with empty `exact_findings`, `rank_set_only_in_reference`,
  `rank_set_only_in_candidate`, and `hsp_drift`. This is the smoke test for the comparator
  itself.
3. **Taxonomic exclusion.** The query source accession must not appear as a canonical subject.
  Same-RID XML2 is also parsed descriptor by descriptor; every descriptor must carry a taxid. A
  pinned NCBI Taxonomy response covers every unique result taxid and proves the excluded taxid and
  descendants are absent. Fresh ORF1ab XML2 showed why title inference was unsafe: core_nt grouped
  three taxid `2697049` accessions with synthetic construct `MT108784` (taxid `32630`). The
  corrected dual-NOT request excludes both the requested taxon and that mixed-group alias, after
  which all 643 descriptors are taxid-bearing and the forbidden descendant count is zero.
4. **Canonical-field guard.** The dashboard's reusable
   [`parse_blast_xml`](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/blast/results_parser.py)
  (which feeds the UI, API, and CSV export) must emit the same number of rows and agree with the
  comparator on all 23 projected fields for every HSP. This includes canonical versioned subject
  IDs from `Hit_id`; NCBI XML can carry a different `Hit_accession` when one hit groups multiple
  deflines. If `parse_blast_xml` drops, reorders, or substitutes a field, the test fails before
  the dashboard misrepresents NCBI's output.
5. **Candidate-vs-reference parity (opt-in).** Set `ELB_PARITY_CANDIDATE_DIR=<path>` and the
  test layer compares every reference XML against `<path>/<gene_id>.xml(.gz)` and asserts
  `compare_summaries(...).exact_equivalent == True`. Strict comparison covers BLAST version and
  parameters, subject rank/identity/length, every HSP's raw score, bit score, e-value, identity,
  positives, gaps, coordinates, frames, aligned sequences/midline, and all search statistics.
  Query-specific `hsp-len` and `eff-space` are enriched from same-RID XML2 when XML1 reports zero.
  When the independent release/count proof matches, a local 64-bit filtered database length may
  compare to NCBI XML1's exact modulo-$2^{32}$ representation; the report sets
  `db_len_representation_normalized=true`. Without that external proof the same difference remains
  snapshot drift and fails exact parity.
  DB snapshot drift is auto-detected from `Statistics_db-num` / `db-len`; it may produce a
  separate candidate-within-reference diagnostic, but can never satisfy the exact gate.

The legacy CLI comparison scripts in `scripts/dev/` are still available for ad-hoc operator
use:

- [`compare-blast-xml.py`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/compare-blast-xml.py) -- apples-to-apples XML comparison between two BLAST+ runs.
- [`compare-blast-web-xml-outfmt6.py`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/compare-blast-web-xml-outfmt6.py) -- NCBI Web BLAST XML against our outfmt 6 rows.
- [`compare-blast-web-csv.py`](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/compare-blast-web-csv.py) -- NCBI Web BLAST CSV export against our outfmt 6 rows.

## Run the parity validation locally

The full parity suite runs against checked-in fixtures only -- no Azure resources, no NCBI
network access, no live BLAST+ run.

```bash
uv run pytest -q api/tests/test_web_blast_parity_fixtures.py \
                   api/tests/test_web_blast_parity_xml.py
```

This is the default local validation path. The candidate-vs-reference layer in the XML test file
skips cleanly when `ELB_PARITY_CANDIDATE_DIR` is unset, so CI stays green even though the layer
still provides the harness operators need for real ElasticBLAST runs.

### Validate an actual ElasticBLAST run against the references

After running an ElasticBLAST job for each reference gene, drop the resulting XML outputs into a
single directory named `<gene_id>.xml` (or `.xml.gz`) and re-run the XML test suite with the env
var pointed at it:

```bash
# directory layout the test layer expects:
#   /tmp/my-blast-run/f3l.xml.gz
#   /tmp/my-blast-run/rrna_18s.xml.gz
#   /tmp/my-blast-run/rdrp_orf1ab.xml.gz

ELB_PARITY_CANDIDATE_DIR=/tmp/my-blast-run \
  uv run pytest -q api/tests/test_web_blast_parity_xml.py
```

When `ELB_PARITY_CANDIDATE_DIR` is unset, all three live checks skip cleanly. Once it is set, all
three candidate files are mandatory: a missing gene fails the run instead of silently reducing
coverage. Any divergence fails with a structured diff (comparison mode, DB snapshot drift flag,
accession-only-in-reference, accession-only-in-candidate, and full subject/HSP drift samples). A
cross-snapshot containment pass is useful diagnostic evidence but is not reported as exact parity.

## Refresh NCBI reference XML (opt-in, never in CI)

NCBI RIDs expire after roughly 36 hours. To pin a new RID's XML output, or to re-pull a reference
XML before its retention window ends:

```bash
# Polls SearchInfo until READY, then downloads the XML to the fixture path.
uv run python scripts/dev/fetch-ncbi-blast-rid.py \
  --rid 1FZVPFJ6014 \
  --out api/tests/fixtures/web_blast_parity/reference_xml/f3l_1FZVPFJ6014.xml
```

The fetcher is intentionally opt-in:

- It is not invoked by `pytest`, `azd`, or any CI workflow.
- It is rate-friendly: default 30 s poll interval, 30 min budget, sequential by design.
- It refuses to overwrite the target file with a non-XML body, so a stale NCBI HTML error page can
  never silently replace a good reference.

Once a reference XML is present under
`api/tests/fixtures/web_blast_parity/reference_xml/`, run the diff harness against a corresponding
BLAST+ run from this dashboard:

```bash
uv run python scripts/dev/compare-blast-web-xml-outfmt6.py \
  --web-xml api/tests/fixtures/web_blast_parity/reference_xml/f3l_1FZVPFJ6014.xml \
  --candidate /path/to/job-output.outfmt6 \
  --json /tmp/f3l-parity-report.json
```

A non-zero exit code or `equivalent: false` in the report is a parity regression — investigate
before claiming the run is Web BLAST-equivalent.

## Database parity

Byte-level result equality is only meaningful when both runs see the same `core_nt` snapshot. The
verified default search-space metadata lives in
[`api/services/web_blast_searchsp.py`](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/web_blast_searchsp.py).
When the dashboard's local `core_nt` snapshot is older or newer than NCBI Web BLAST's, expect
differences in hit membership and HSP/search statistics. The report records
`comparison_mode=drift_tolerant_containment` and a `drift_compatible` diagnostic, while
`exact_equivalent` remains false. Only `exact_equivalent=true` is acceptable evidence for complete
parity. See the [Compatibility Plan §8 Equivalence Evidence Matrix](../research/web-blast-compatibility-plan.md#stage-8-equivalence-evidence-matrix)
for the full database-version policy.

The 2026-09-04 authoritative proof does not rely on XML1's filtered/wrapped database length. The
Web BLAST UI's `getDBInfo.cgi` reports update date `2026/08/19` and 130,155,243 sequences. NCBI v5
metadata independently reports release `2026-08-19`, the same sequence count, 998,069,435,926
letters, and 84 volumes. The deployed active generation
`ncbi-direct-20260819-cab30d18c360` carries that release and both full counts, with a complete
same-generation 10/10 database-order oracle.

Each live request must also carry the same-RID XML2 effective search space and its validated
taxonomy-filtered statistical context. Web BLAST reports the length-adjusted effective space in
XML statistics, but HSP E-values use the effective query length times the raw filtered database
length. OpenAPI 4.46 therefore passes the filtered length as `-dbsize`, the HSP scoring space as
`-searchsp`, and a private immutable manifest to the merger. The merger validates that manifest
against the active generation and runtime flags before reconstructing query-level statistics; it
never rewrites HSP scores or E-values. The 64-nt calibration shown in the database catalogue is
only a fallback for requests without query-specific evidence.

## Outstanding gaps tracked by issue #8

- Fresh same-snapshot references now exist for all three genes. The candidate gate remains blocked
  until all three live ElasticBLAST XML files report
  `exact_equivalent=true` and `exact_findings=[]`.
- A live run must verify the deployed API/frontend/terminal/OpenAPI versions, Result Passport,
  dashboard/API/export field parity, and a clean App Insights window before issue closure.
