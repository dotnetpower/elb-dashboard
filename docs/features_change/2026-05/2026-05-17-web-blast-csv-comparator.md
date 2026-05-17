# Web BLAST CSV Comparator

## Motivation

Two NCBI Web BLAST CSV exports for the Monkeypox F3L query were added under `docs/temp/`. They provide a useful Web result oracle, but they need a repeatable comparison path against sharded BLAST tabular output.

## User-facing change

No product UI behavior changed. A dev validation helper now compares Web BLAST CSV exports with BLAST outfmt 6 candidate output and emits a JSON report covering accession overlap, order, and value-field mismatches.

## API/IaC diff summary

- Added `scripts/dev/compare-blast-web-csv.py`.
- Added focused tests in `api/tests/test_compare_blast_web_csv.py`.
- Recorded MPXV F3L Web CSV vs sibling v3 sharded negative evidence in `docs/blast-searchsp-discovery.md`.

## Validation evidence

- `uv run ruff check scripts/dev/compare-blast-web-csv.py api/tests/test_compare_blast_web_csv.py`
- `uv run pytest -q api/tests/test_compare_blast_web_csv.py`
- `scripts/dev/compare-blast-web-csv.py --web-csv docs/temp/blast_inclusive_F3L_928998.csv --candidate ~/dev/elastic-blast-azure/benchmark/results/v3/raw/B1-S10/merged_all.out --query-id NC_063383.1:c46483-46022 --json docs/temp/f3l-web-inclusive-vs-v3-sharded-summary.json`
- `scripts/dev/compare-blast-web-csv.py --web-csv docs/temp/blast_exclusive_F3L_928998.csv --candidate ~/dev/elastic-blast-azure/benchmark/results/v3/raw/B1-S10/merged_all.out --query-id NC_063383.1:c46483-46022 --json docs/temp/f3l-web-exclusive-vs-v3-sharded-summary.json`

Both CSV-vs-v3 comparisons correctly report `equivalent=false`, `shared_accessions=0`, and `top10_overlap=0`, showing the current Web CSV exports do not match the older v3 benchmark database snapshot/source.
