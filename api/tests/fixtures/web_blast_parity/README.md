# Web BLAST parity fixtures

Reference inputs used by
[`api/tests/test_web_blast_parity_fixtures.py`](../../test_web_blast_parity_fixtures.py) and
[`api/tests/test_web_blast_parity_xml.py`](../../test_web_blast_parity_xml.py) to prove that an
NCBI Web BLAST form request is mapped into the same BLAST+ command-line options by this
dashboard's `generate_config()` builder, and that the resulting BLAST XML matches the captured
NCBI Web BLAST reference XML for every reference gene. This fixture set tracks issue
[#8](https://github.com/dotnetpower/elb-dashboard/issues/8) ("Validate BLAST result parity with
NCBI Web BLAST references").

## Reference genes

| Gene | Pathogen | Query length | NCBI RID (captured) | Entrez exclusion | Status |
| --- | --- | --- | --- | --- | --- |
| F3L | Monkeypox virus (`taxid=10244`) | 462 bp | `1FZVPFJ6014` | `NOT txid3431483[ORGN]` | FASTA + payload + reference XML captured |
| 18S ribosomal RNA | Plasmodium falciparum (`taxid=5833`) | 2,151 bp | `1FZW35EN014` | `NOT txid5833[ORGN]` (P. falciparum itself) | FASTA + payload + reference XML captured |
| RdRp / ORF1ab | SARS-CoV-2 (`taxid=2697049`) | 21,290 bp | `1G7Z8G7W016` | `NOT txid3418604[ORGN]` | FASTA + payload + reference XML captured |

All three genes are now fully captured. The RdRp / ORF1ab FASTA was pulled from NCBI Entrez
`efetch` against `NC_045512.2:266-21555`, which matches the issue body exactly.

## Files

- `f3l_query.fasta` -- Monkeypox virus F3L gene reference query.
- `18s_query.fasta` -- Plasmodium falciparum 18S ribosomal RNA reference query.
- `orf1ab_query.fasta` -- SARS-CoV-2 RdRp / ORF1ab reference query (`NC_045512.2:266-21555`,
  pulled from NCBI Entrez `efetch`).
- `reference_payloads.json` -- the full NCBI Web BLAST form payload per gene, in two forms:
  - `ncbi_form` -- the literal HTTP form fields a `POST https://blast.ncbi.nlm.nih.gov/blast/Blast.cgi`
    would send (`PROGRAM`, `DATABASE`, `MEGABLAST`, `HITLIST_SIZE`, `EXPECT`, `WORD_SIZE`, `FILTER`,
    `ENTREZ_QUERY`, ...). This is the source of truth for "what the user asked NCBI for".
  - `dashboard_request` -- the structured submit payload this dashboard accepts at `/api/blast/...`
    (`program`, `db`, `evalue`, `word_size`, `max_target_seqs`, `low_complexity_filter`,
    `taxid`, `is_inclusive`, ...). The contract test asserts that mapping `ncbi_form -> dashboard_request`
    produces a `generate_config()` INI whose `[blast].options` line contains every BLAST+ flag the
    NCBI form implies.
  - `expected_blast_options` -- the exact BLAST+ flags that must appear in the `[blast].options`
    line for the equivalence claim to hold (`-evalue 0.05`, `-word_size 28`,
    `-max_target_seqs 500`, `-dust yes`, `-negative_taxids <N>`).
  - `reference_xml_path` -- repo-relative path to the captured NCBI Web BLAST reference XML for
    this gene (always gzip-compressed `.xml.gz`).
  - `core_nt_snapshot` -- top-level policy block declaring how the comparator treats `core_nt`
    snapshot drift between reference and candidate XML. The comparator auto-detects drift via
    `Statistics_db-num` / `db-len` and downgrades the comparison from per-HSP equality to
    accession rank-set equality when drift is present.
- `reference_xml/` -- captured NCBI Web BLAST reference XML for every gene, gzip-compressed to
  keep the repo lean:
  - `f3l_1FZVPFJ6014.xml.gz` (350 hits)
  - `rrna_18s_1FZW35EN014.xml.gz` (500 hits, HITLIST_SIZE cap)
  - `rdrp_orf1ab_1G7Z8G7W016.xml.gz` (500 hits, HITLIST_SIZE cap)
  The comparator at `api/services/blast/web_blast_parity.py::parse_summary` reads `.xml` and
  `.xml.gz` transparently.

## Mapping policy

- `PROGRAM=blastn` + `MEGABLAST=on` → dashboard submits `program=blastn`. Modern BLAST+ defaults the
  `blastn` task to `megablast` when `-task` is not supplied, so the megablast invocation is
  reached implicitly. Future work: expose a typed `task` field if `MEGABLAST=off` reference cases
  are added.
- `DATABASE=core_nt` → `db = blast-db/core_nt/core_nt` (database snapshot must be the same NCBI
  version for byte-level comparison; the verified default lives in
  `api/services/web_blast_searchsp.py`).
- `FORMAT_TYPE=XML` → result requested as XML for the comparison harness. The dashboard's
  submit/result UI is `outfmt=6` by default; comparison fixtures should normalise through
  [`scripts/dev/compare-blast-web-xml-outfmt6.py`](../../../../scripts/dev/compare-blast-web-xml-outfmt6.py).
- `HITLIST_SIZE=500` → `max_target_seqs = 500`.
- `EXPECT=0.05` → `evalue = 0.05`.
- `WORD_SIZE=28` → `word_size = 28`.
- `FILTER=L` (low-complexity masking on) → `low_complexity_filter = true`, which `generate_config()`
  renders as `-dust yes -soft_masking false`.
- `ENTREZ_QUERY=NOT txid<N>[ORGN]` → `taxid = N`, `is_inclusive = false`, which renders as
  `-negative_taxids <N>`. This is a structural negative-taxid filter; the exclusion is enforced by
  BLAST+ at the database level, not by post-filtering the NCBI Entrez query string.

## Refreshing reference XML from NCBI (opt-in)

The reference RIDs in the table above expire after the NCBI retention window. To pull a fresh
reference XML before the RID expires, or to re-pin a new RID:

```bash
uv run python scripts/dev/fetch-ncbi-blast-rid.py \
  --rid 1FZVPFJ6014 \
  --out api/tests/fixtures/web_blast_parity/reference_xml/f3l_1FZVPFJ6014.xml
```

This live-mode helper is **not** run by CI and is **not** the default validation path. It exists
so a maintainer can manually refresh the reference XML and check it in before running the
end-to-end XML comparison harness against a real BLAST+ run on this dashboard.
