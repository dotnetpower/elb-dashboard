# Search Space Default Hardening

## Motivation

The dashboard must only auto-apply Web BLAST-compatible `searchsp` defaults when the value is backed by verified evidence. Storage metadata can also contain `effective_search_space`, but that value may come from local calibration, sharding metadata, or custom database preparation and must not be labeled as a verified Web BLAST default.

## User-facing change

`/api/blast/databases` now separates storage metadata search-space values from verified Web BLAST defaults:

- `web_blast_searchsp` is emitted only for databases present in the verified defaults table.
- Storage metadata values are emitted as `db_effective_search_space` with `db_effective_search_space_source = storage_metadata`.
- Unknown/custom databases no longer appear in the submit UI as having a verified Web BLAST calibration default just because their metadata contains `effective_search_space`.

## API / IaC diff summary

- Hardened `api.services.storage_data.list_databases()` so metadata-derived values cannot populate `web_blast_searchsp`.
- Added regression coverage for `core_nt` verified defaults and an unverified custom DB metadata value.
- Redacted unnecessary NCBI HTML response prefixes from Web BLAST evidence JSON while preserving RID and summary evidence.
- No IaC changes.

## Validation evidence

- `uv run ruff check api/services/storage_data.py api/tests/test_storage_data.py`
- `uv run pytest -q api/tests/test_storage_data.py api/tests/test_blast_submit_route_options.py api/tests/test_blast_databases_warmup_plan.py` -> 16 passed
- `python -m json.tool .../web-submit*.json` and grep for raw NCBI session/phid markers -> clean
