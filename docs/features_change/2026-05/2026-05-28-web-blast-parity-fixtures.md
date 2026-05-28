# Web BLAST parity fixtures and request-mapping contract test

## Motivation
Issue [#8](https://github.com/dotnetpower/elb-dashboard/issues/8) asks us to
prove that BLAST jobs run through this dashboard produce results equivalent to
NCBI Web BLAST for three reference diagnostic genes — F3L (Monkeypox virus),
18S ribosomal RNA (Plasmodium falciparum), and RdRp / ORF1ab (SARS-CoV-2).

The existing compatibility infrastructure (`api/services/blast/compatibility.py`,
`api/services/blast/equivalence_evidence.py`, `scripts/dev/compare-blast-*.py`)
already covers the contract / evidence registry / hit-comparison primitives, but
two pieces were still missing:

1. A checked-in fixture set with the actual reference FASTA + the NCBI form
   payload that the dashboard must mirror, so the form→INI mapping is locked
   down and refactor-resistant.
2. A CI-runnable contract test that fails the moment the dashboard's
   `generate_config()` stops emitting the expected BLAST+ flags for those NCBI
   form values.

Without those two pieces, the parity claim depended on someone manually
re-running the comparison scripts, which is exactly the workflow issue #8 is
trying to remove.

## User-facing change
- New developer / user documentation page **Web BLAST Parity Validation**
  (linked from User Guide nav) describing the reference genes, the NCBI form →
  dashboard request → BLAST+ flag mapping, how to run the offline parity
  contract test, and how to opt in to refreshing the NCBI reference XML.
- The page also makes the outstanding gaps tracked by issue #8 explicit:
  RdRp / ORF1ab FASTA capture, live `core_nt` snapshot pinning, and a
  result-side regression test wave once a fresh reference XML is captured.
- No UI / API behavior change.

## API / IaC diff summary
None. Fixtures, test, doc page, and an opt-in dev script only.

## Files touched
- `api/tests/fixtures/web_blast_parity/README.md` *(new)* — explains the
  fixture set, the form→flag mapping, and the refresh procedure.
- `api/tests/fixtures/web_blast_parity/f3l_query.fasta` *(new)* — 462 bp F3L
  Monkeypox reference query.
- `api/tests/fixtures/web_blast_parity/18s_query.fasta` *(new)* — 2,151 bp 18S
  ribosomal RNA Plasmodium falciparum reference query.
- `api/tests/fixtures/web_blast_parity/reference_payloads.json` *(new)* —
  canonical NCBI form payload, dashboard submit payload, expected BLAST+ flag
  list, query length, captured RID, and exclusion taxid for each gene; RdRp /
  ORF1ab tracked under a `blockers` section.
- `api/tests/test_web_blast_parity_fixtures.py` *(new)* — 10 tests asserting
  fixture shape, FASTA length parity with the payload, dashboard request
  mirroring of the NCBI form, that `generate_config()` emits the expected
  BLAST+ flags (`-evalue`, `-word_size`, `-max_target_seqs`, `-dust yes`,
  `-soft_masking false`, `-negative_taxids`), and that inclusive taxid
  filtering is not silently introduced.
- `scripts/dev/fetch-ncbi-blast-rid.py` *(new)* — opt-in developer utility
  that polls SearchInfo until READY then downloads the XML for a given RID
  into the fixture directory. Never run by CI; never imported by production
  code.
- `docs/user-guide/web-blast-parity.md` *(new)* — user-facing validation guide.
- `docs/research/web-blast-compatibility-plan.md` — Stage 8 ledger now marks
  the comparator-fixture task partial and lists the RdRp / ORF1ab capture as
  the open follow-up.
- `mkdocs.yml` — nav entry for the new user-guide page.

## Validation
- `uv run pytest -q api/tests/test_web_blast_parity_fixtures.py` → **10 passed
  in 2.23s**.
- FASTA length parity verified independently: f3l_query.fasta = 462 bp,
  18s_query.fasta = 2151 bp (matches issue #8 body).
- `uv run python scripts/docs/check_frontmatter.py` → **OK — frontmatter
  guard checked 48 navigated pages.**
- `uv run python scripts/dev/fetch-ncbi-blast-rid.py --help` → usage prints
  cleanly.

## Outstanding gaps (issue #8 stays open)
- RdRp / ORF1ab FASTA + a reproducible RID still need to be captured.
- Live `core_nt` snapshot pinning between this dashboard and NCBI Web BLAST is
  cluster lifecycle work, not test-suite work.
- Result-side XML/CSV regression tests over the two captured genes will be
  added once a fresh reference XML is pulled with the new fetcher.
