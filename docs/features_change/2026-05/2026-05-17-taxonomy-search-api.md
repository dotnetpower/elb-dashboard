# Taxonomy search API

## Motivation

The submit UI needs a backend-owned way to search NCBI organism names and resolve them to taxonomy IDs before adding frontend taxonomy filter controls.

## User-facing change

No frontend controls are enabled in this change. Authenticated callers can query `/api/blast/taxonomy/search` with an organism name or numeric taxid and receive ranked NCBI taxonomy candidates.

## API/IaC diff summary

- Added `api.services.taxonomy` as an NCBI E-utilities proxy with query validation, timeout handling, optional `NCBI_TOOL` / `NCBI_EMAIL` / `NCBI_API_KEY` forwarding, and a 24-hour in-process TTL cache.
- Added `GET /api/blast/taxonomy/search?q=<name-or-taxid>&limit=<1-20>`.
- The legacy `POST /api/blast/taxonomy` Lab Tool endpoint remains a 503 stub; the new search endpoint is scoped to submit taxonomy filters.
- No IaC changes.

## Validation evidence

- `uv run pytest -q api/tests/test_taxonomy_search.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py` — 65 passed.
- `uv run ruff check api/services/taxonomy.py api/tests/test_taxonomy_search.py api/services/blast_config.py api/tests/test_blast_config_sharding.py api/tests/test_blast_submit_route_options.py api/tests/test_external_blast_api.py` — passed.
- `git --no-pager diff --check -- api/services/taxonomy.py api/routes/stubs.py api/tests/test_taxonomy_search.py docs/features_change/2026-05/2026-05-17-taxonomy-search-api.md` — passed.