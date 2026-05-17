# Backend BLAST taxid filter

## Motivation

Users need to limit BLAST searches to a requested NCBI taxonomy ID or exclude that taxonomy ID before the frontend taxonomy controls are added.

## User-facing change

No frontend controls are enabled in this change. Backend submit payloads can now carry `taxid` with `is_inclusive` and the generated ElasticBLAST config renders the matching BLAST+ taxonomy filter.

## API/IaC diff summary

- `/api/blast/jobs` and `/api/blast/submit` option normalization now preserve top-level `taxid` and `is_inclusive` values into the task options payload.
- ElasticBLAST config generation renders `taxid` as `-taxids <id>` when inclusive and `-negative_taxids <id>` when exclusive.
- Split-child submit option forwarding preserves the same taxonomy filter for query-group submissions.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py` — 58 passed.
- `uv run ruff check api/routes/stubs.py api/tasks/blast.py api/services/blast_config.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py` — blocked by pre-existing `B008` findings in `api/routes/stubs.py`; the new backend taxonomy filter code is covered by the focused pytest suite above.
- `uv run ruff check api/services/blast_config.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py` — passed.