"""NCBI taxonomy services subpackage (split from taxonomy.py)."""

from __future__ import annotations

from api.services.taxonomy.cache import (
    clear_taxonomy_cache,
    clear_taxonomy_detail_cache,
    clear_taxonomy_siblings_cache,
)
from api.services.taxonomy.detail import fetch_taxonomy_detail
from api.services.taxonomy.search import (
    TaxonomySearchUnavailable,
    search_taxonomy,
)
from api.services.taxonomy.siblings import fetch_taxonomy_tree

__all__ = [
    "TaxonomySearchUnavailable",
    "clear_taxonomy_cache",
    "clear_taxonomy_detail_cache",
    "clear_taxonomy_siblings_cache",
    "fetch_taxonomy_detail",
    "fetch_taxonomy_tree",
    "search_taxonomy",
]
