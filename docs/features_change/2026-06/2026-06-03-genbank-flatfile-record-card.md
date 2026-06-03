# GenBank flat-file record card on the Sequence Detail page

## Motivation

A researcher compared the dashboard's Sequence Detail page (`/sequence/:accession`)
with the public NCBI nuccore page for accession `OZ254605.1` and noted that NCBI
leads with the canonical GenBank flat-file header block
(`LOCUS / DEFINITION / ACCESSION / VERSION / DBLINK / KEYWORDS / SOURCE / ORGANISM`),
while the dashboard only showed the same data scattered across structured cards.
They asked whether the dashboard could reproduce that familiar layout.

## User-facing change

* The Sequence Detail page now renders a **"GenBank record"** card directly under
  the header summary. It reproduces the NCBI flat-file header block verbatim in a
  monospace block with the classic 12-column tag field, 79-column line width, and
  continuation lines indented to column 13.
* The block is composed from data the page already fetches via `getNuccoreGenBank`
  (LOCUS line from locus/length/moltype/topology/division/update_date, wrapped
  DEFINITION, ACCESSION, VERSION, DBLINK from `xrefs`, KEYWORDS, SOURCE, and the
  ORGANISM sub-keyword with the wrapped taxonomy lineage).
* The existing structured cards (Sample & source, Taxonomy, Features, References,
  FASTA preview) are unchanged — the flat-file card is purely additive.

## API / IaC diff summary

* **Backend** (`api/services/ncbi/nuccore.py`): added `_parse_keywords` to parse the
  `GBSeq_keywords` → `GBKeyword` list (cap 32, each truncated to 120 chars) and a new
  `keywords: list[str]` field in the `fetch_nuccore_genbank` return dict. This was the
  only GenBank header field not already exposed, so KEYWORDS now renders faithfully
  (`.` when empty, matching the GenBank convention). No route/model change — the
  `/ncbi/nuccore/{accession}/genbank` route returns the dict directly.
* **Frontend types** (`web/src/api/ncbi.ts`): added `keywords: string[]` to the
  `NuccoreGenBank` interface.
* **Frontend page** (`web/src/pages/sequence/SequenceDetail.tsx`): added module-level
  `genbankTag` / `genbankWrap` / `genbankFlatLines` helpers, a `flatRecord` memo, and
  the "GenBank record" glass card.
* No IaC change.

## Validation evidence

* `uv run pytest -q api/tests/test_ncbi_nuccore.py` → 52 passed (includes the updated
  `test_fetch_nuccore_genbank_parses_record` asserting `keywords == ["RefSeq", "MANE Select"]`
  against the extended `GBSeq_keywords` fixture).
* `uv run ruff check api/services/ncbi/nuccore.py api/tests/test_ncbi_nuccore.py` → clean.
* `cd web && npm run build` → built successfully (`SequenceDetail-*.js` emitted).
