"""NCBI taxonomy lineage + siblings fetcher."""

from __future__ import annotations

import logging
from typing import Any

from api.services.taxonomy import search as _search_mod
from api.services.taxonomy.cache import (
    _MAJOR_RANKS_SET,
    _siblings_cache_get,
    _siblings_cache_put,
)
from api.services.taxonomy.detail import (
    fetch_taxonomy_detail,
)
from api.services.taxonomy.search import (
    MAX_RESULTS,
    TaxonomySearchUnavailable,
)

LOGGER = logging.getLogger(__name__)


def _fetch_siblings_at_rank(
    parent_taxid: int,
    rank: str,
    *,
    limit: int = 5,
    exclude_taxid: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch taxa under *parent_taxid* at a specific *rank*.

    Uses ``esearch txid<parent>[Subtree] AND "<rank>"[Rank]`` then
    ``esummary`` for names.  Results are cached 24 h per
    ``(parent_taxid, rank, limit)``.  Returns ``(rows, was_cached)``.
    """
    # Clamp limit defensively — the route already enforces 1..8 but the
    # service is also called from tests and other internal callers.
    safe_limit = max(1, min(int(limit), MAX_RESULTS))
    cache_key = (parent_taxid, rank.lower(), safe_limit)
    cached = _siblings_cache_get(cache_key)
    was_cached = cached is not None
    if cached is not None:
        rows = cached
    else:
        term = f'txid{parent_taxid}[Subtree] AND "{rank}"[Rank]'
        # Fetch a few extras so we still have *safe_limit* after filtering.
        taxids = _search_mod._search_taxids(term, min(safe_limit + 5, MAX_RESULTS))
        rows = []
        if taxids:
            summaries = _search_mod._summarise_taxids(taxids, query="")
            rows = [
                {
                    "taxid": s["taxid"],
                    "scientific_name": s["scientific_name"],
                    "rank": s["rank"],
                }
                for s in summaries
            ]
        _siblings_cache_put(cache_key, rows)

    if exclude_taxid is not None:
        rows = [r for r in rows if r["taxid"] != exclude_taxid]
    return rows[:safe_limit], was_cached


def fetch_taxonomy_tree(
    taxid: int,
    *,
    sibling_limit: int = 3,
) -> dict[str, Any]:
    """Build a tree payload: full lineage + siblings at each major rank.

    The response shape::

        {
          "taxid": 9606,
          "lineage": [...],          # same as lineage_ex + self
          "siblings": {              # keyed by parent taxid (str)
            "33208": [{taxid, scientific_name, rank}, ...],
            ...
          },
          "cached": false,
          "source": "ncbi_eutils"
        }
    """
    detail = fetch_taxonomy_detail(taxid)
    lineage_ex: list[dict[str, Any]] = detail["lineage_ex"]

    # Append the selected organism itself (lineage_ex only has ancestors).
    lineage = [
        *lineage_ex,
        {
            "taxid": detail["taxid"],
            "scientific_name": detail["scientific_name"],
            "rank": detail["rank"],
        },
    ]

    # Identify major-rank nodes and their predecessors.
    major_indices: list[int] = []
    for i, node in enumerate(lineage):
        if node["rank"].lower() in _MAJOR_RANKS_SET:
            major_indices.append(i)

    siblings: dict[str, list[dict[str, Any]]] = {}
    all_cached = bool(detail.get("cached", False))
    any_sibling_lookup = False

    for idx in major_indices:
        node = lineage[idx]
        # Find the predecessor: walk backwards to the previous major-rank.
        parent: dict[str, Any] | None = None
        for j in range(idx - 1, -1, -1):
            if lineage[j]["rank"].lower() in _MAJOR_RANKS_SET:
                parent = lineage[j]
                break
        if parent is None:
            # Topmost major rank (superkingdom) — no parent to query.
            continue

        any_sibling_lookup = True
        try:
            sibs, was_cached = _fetch_siblings_at_rank(
                parent["taxid"],
                node["rank"],
                limit=sibling_limit,
                exclude_taxid=node["taxid"],
            )
            if not was_cached:
                all_cached = False
            if sibs:
                siblings[str(parent["taxid"])] = sibs
        except TaxonomySearchUnavailable:
            all_cached = False
            LOGGER.debug(
                "siblings lookup failed for taxid=%s rank=%s",
                parent["taxid"],
                node["rank"],
            )

    if not any_sibling_lookup:
        # Nothing to fetch beyond the detail; honour the detail cache state.
        pass

    return {
        "taxid": detail["taxid"],
        "scientific_name": detail["scientific_name"],
        "rank": detail["rank"],
        "lineage": lineage,
        "siblings": siblings,
        "cached": all_cached,
        "source": "ncbi_eutils",
    }
