"""NCBI taxonomy services subpackage (split from taxonomy.py).

Responsibility: Keep the historical `api.services.taxonomy` import surface while
routing implementation to focused submodules (`search`, `detail`, `siblings`,
`cache`).
Edit boundaries: This package facade owns compatibility wrappers only. Put search
logic in `search.py`, detail/XML logic in `detail.py`, tree/sibling logic in
`siblings.py`, and cache primitives in `cache.py`.
Key entry points: `search_taxonomy`, `fetch_taxonomy_detail`, `fetch_taxonomy_tree`,
cache clear helpers, and private helper names that tests historically patch.
Risky contracts: Package-level monkeypatches must be forwarded into the submodule
that performs the lookup before calling the implementation.
Validation: `uv run pytest -q api/tests/test_taxonomy_search.py`.
"""

from __future__ import annotations

from typing import Any

import httpx

from api.services.taxonomy import cache as _cache_module
from api.services.taxonomy import detail as _detail_module
from api.services.taxonomy import search as _search_module
from api.services.taxonomy import siblings as _siblings_module

TaxonomySearchUnavailable = _search_module.TaxonomySearchUnavailable
DEFAULT_TIMEOUT_SECONDS = _search_module.DEFAULT_TIMEOUT_SECONDS
EUTILS_BASE_URL = _search_module.EUTILS_BASE_URL
MAX_EFETCH_BYTES = _detail_module.MAX_EFETCH_BYTES
MAX_ESUMMARY_BYTES = _search_module.MAX_ESUMMARY_BYTES
MAX_QUERY_CHARS = _search_module.MAX_QUERY_CHARS
MAX_RESULTS = _search_module.MAX_RESULTS

# Compatibility attributes: tests and some callers monkey-patch these on the
# package (`api.services.taxonomy`) because the previous implementation was a
# single flat module. The wrappers below copy these values into the submodule
# that actually resolves the global name before delegating.
_request_json = _search_module._request_json
_search_taxids = _search_module._search_taxids
_summarise_taxids = _search_module._summarise_taxids
_request_bytes = _detail_module._request_bytes

clear_taxonomy_cache = _cache_module.clear_taxonomy_cache
clear_taxonomy_detail_cache = _cache_module.clear_taxonomy_detail_cache
clear_taxonomy_siblings_cache = _cache_module.clear_taxonomy_siblings_cache


def _sync_search_patch_surface() -> None:
    _search_module._request_json = _request_json
    _search_module._search_taxids = _search_taxids
    _search_module._summarise_taxids = _summarise_taxids
    _search_module.httpx = httpx


def _sync_detail_patch_surface() -> None:
    _detail_module._request_bytes = _request_bytes
    _detail_module.httpx = httpx


def search_taxonomy(query: str, *, limit: int = 10) -> dict[str, Any]:
    _sync_search_patch_surface()
    return _search_module.search_taxonomy(query, limit=limit)


def fetch_taxonomy_detail(taxid: int) -> dict[str, Any]:
    _sync_detail_patch_surface()
    return _detail_module.fetch_taxonomy_detail(taxid)


def fetch_taxonomy_tree(taxid: int, *, sibling_limit: int = 3) -> dict[str, Any]:
    _sync_search_patch_surface()
    _sync_detail_patch_surface()
    _siblings_module.fetch_taxonomy_detail = fetch_taxonomy_detail
    _siblings_module._search_mod = _search_module
    return _siblings_module.fetch_taxonomy_tree(taxid, sibling_limit=sibling_limit)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EUTILS_BASE_URL",
    "MAX_EFETCH_BYTES",
    "MAX_ESUMMARY_BYTES",
    "MAX_QUERY_CHARS",
    "MAX_RESULTS",
    "TaxonomySearchUnavailable",
    "_request_bytes",
    "_request_json",
    "_search_taxids",
    "_summarise_taxids",
    "clear_taxonomy_cache",
    "clear_taxonomy_detail_cache",
    "clear_taxonomy_siblings_cache",
    "fetch_taxonomy_detail",
    "fetch_taxonomy_tree",
    "httpx",
    "search_taxonomy",
]
