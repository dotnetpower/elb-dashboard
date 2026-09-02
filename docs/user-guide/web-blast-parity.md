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
| F3L | Monkeypox virus (`taxid=10244`) | 462 bp | `1FZVPFJ6014` | `NOT txid3431483[ORGN]` (`Orthopoxvirus monkeypox`, species) | `reference_xml/f3l_1FZVPFJ6014.xml.gz` |
| 18S ribosomal RNA | Plasmodium falciparum (`taxid=5833`) | 2,151 bp | `1FZW35EN014` | `NOT txid5833[ORGN]` (P. falciparum itself) | `reference_xml/rrna_18s_1FZW35EN014.xml.gz` |
| RdRp / ORF1ab | SARS-CoV-2 (`taxid=2697049`) | 21,290 bp | `1G7Z8G7W016` | `NOT txid3418604[ORGN]` (`Betacoronavirus pandemicum`, species) | `reference_xml/rdrp_orf1ab_1G7Z8G7W016.xml.gz` |

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
3. **Taxonomic exclusion.** The query's own NCBI source accession (e.g. `NC_045512.2` for RdRp)
  must not appear as a canonical subject. The fixture also records the authoritative NCBI species
  name and rank. Organism/taxid absence is not yet fully proven for ORF1ab: captured rank 3
  `MN996528.1` belongs to taxid `2697049`, whose lineage includes excluded species taxid
  `3418604`, but the XML hit groups it with non-excluded identical-sequence deflines. BLAST XML v1
  carries no per-defline taxids, so a fresh taxid-bearing result is required to resolve AC6.
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

## Outstanding gaps tracked by issue #8

- Live `core_nt` snapshot pinning between NCBI Web BLAST and this dashboard is operational work
  that lives in the cluster lifecycle, not in this test suite. The XML comparator reports drift
  diagnostics but deliberately fails the strict exact gate until the snapshots match.
- The 2026-09-02 readiness check confirmed the checked-in references and the active workload DB
  are not the same snapshot. Reference `Statistics_db-num` values are `125,926,199` (F3L),
  `125,832,392` (18S), and `117,842,978` (ORF1ab), while the active
  `ncbi-direct-20260819-cab30d18c360` generation reports `130,155,243` sequences and
  `998,069,435,926` letters. The legacy Web XML v1 references also report only 1.2–1.5 billion
  `db-len` and zero `eff-space`, which cannot identify the full trillion-base DB unambiguously
  (the length is consistent with a wrapped/filtered representation). Starting the ten-node
  cluster cannot make those frozen artifacts exact. Capture fresh Web XML plus authoritative
  full snapshot counts/release identity, pin/download that matching DB generation, and only then
  run the three candidates and require `exact_equivalent=true`.
- ORF1ab exclusion AC6 is separately blocked by the grouped-defline case above. A new reference
  must retain taxids (or be joined to an authoritative accession-taxid snapshot) and prove that no
  returned defline belongs to taxid `3418604` or any descendant.
