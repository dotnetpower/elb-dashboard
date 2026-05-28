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
| F3L | Monkeypox virus (`taxid=10244`) | 462 bp | `1FZVPFJ6014` | `NOT txid3431483[ORGN]` | `reference_xml/f3l_1FZVPFJ6014.xml.gz` |
| 18S ribosomal RNA | Plasmodium falciparum (`taxid=5833`) | 2,151 bp | `1FZW35EN014` | `NOT txid5833[ORGN]` (P. falciparum itself) | `reference_xml/rrna_18s_1FZW35EN014.xml.gz` |
| RdRp / ORF1ab | SARS-CoV-2 (`taxid=2697049`) | 21,290 bp | `1G7Z8G7W016` | `NOT txid3418604[ORGN]` | `reference_xml/rdrp_orf1ab_1G7Z8G7W016.xml.gz` |

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
   `equivalent=True` with empty `findings`, `rank_set_only_in_reference`,
   `rank_set_only_in_candidate`, and `hsp_drift`. This is the smoke test for the comparator
   itself.
3. **Query source exclusion.** The query's own NCBI source accession (e.g. `NC_045512.2` for
   RdRp) must not appear in the hit set. This is the universal taxonomic-exclusion check that
   holds regardless of where NCBI places the excluded taxid in its tree.
4. **Canonical-field guard.** The dashboard's reusable
   [`parse_blast_xml`](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/blast/results_parser.py)
   (which feeds the UI, API, and CSV export) must agree with the comparator on the rank-1 hit's
   subject accession, alignment length, bit score, and e-value. If `parse_blast_xml` ever drops
   a canonical field, this test fails before the dashboard misrepresents NCBI's output.
5. **Candidate-vs-reference parity (opt-in).** Set `ELB_PARITY_CANDIDATE_DIR=<path>` and the
   test layer compares every reference XML against `<path>/<gene_id>.xml(.gz)` and asserts
   `compare_summaries(...).equivalent == True`. DB snapshot drift between candidate and
   reference is auto-detected from `Statistics_db-num` / `db-len` and downgrades the comparison
   from per-HSP equality to accession rank-set equality -- it never silences a real divergence.

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

Any gene whose candidate XML is missing is skipped individually with a clear reason; any gene
whose candidate XML diverges from the reference fails the test with a structured diff (DB
snapshot drift flag, accession-only-in-reference, accession-only-in-candidate, top HSP drift
samples).

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
small differences in hit count tails and e-value precision; the comparison report's `equivalent`
flag will reflect that. See the [Compatibility Plan §8 Equivalence Evidence Matrix](../research/web-blast-compatibility-plan.md#stage-8-equivalence-evidence-matrix)
for the full database-version policy.

## Outstanding gaps tracked by issue #8

- Live `core_nt` snapshot pinning between NCBI Web BLAST and this dashboard is operational work
  that lives in the cluster lifecycle, not in this test suite. The XML comparator already
  auto-detects snapshot drift and downgrades the comparison strictness; pinning the snapshot at
  the cluster layer is what makes the drift-tolerant mode unnecessary.
