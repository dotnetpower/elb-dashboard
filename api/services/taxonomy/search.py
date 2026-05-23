"""NCBI taxonomy search (esummary) + TaxonomySearchUnavailable."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from api.services.taxonomy.cache import (
    _cache_get,
    _cache_put,
)

LOGGER = logging.getLogger(__name__)

EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_QUERY_CHARS = 120
MAX_RESULTS = 20
MAX_ESUMMARY_BYTES = 256 * 1024


class TaxonomySearchUnavailable(RuntimeError):
    """Raised when NCBI taxonomy lookup cannot be completed."""


def search_taxonomy(query: str, *, limit: int = 10) -> dict[str, Any]:
    """Search NCBI Taxonomy by organism name or direct numeric taxid."""
    normalised_query = _normalise_query(query)
    normalised_limit = _normalise_limit(limit)
    cache_key = (normalised_query.lower(), normalised_limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}
    direct_taxid = normalised_query.isdecimal()
    if direct_taxid and int(normalised_query) <= 0:
        raise ValueError("taxonomy taxid must be a positive integer")

    try:
        if direct_taxid:
            taxids = [normalised_query]
        else:
            taxids = _search_taxids(normalised_query, normalised_limit)
        results = _summarise_taxids(taxids, query=normalised_query)
    except TaxonomySearchUnavailable:
        raise
    except Exception as exc:
        raise TaxonomySearchUnavailable("NCBI taxonomy lookup failed") from exc

    payload = {
        "query": normalised_query,
        "count": len(results),
        "source": "ncbi_eutils",
        "cached": False,
        "results": results,
    }
    _cache_put(cache_key, payload)
    return payload


def _normalise_query(query: str) -> str:
    value = " ".join(str(query or "").strip().split())
    if not value:
        raise ValueError("taxonomy query is required")
    if len(value) > MAX_QUERY_CHARS:
        raise ValueError(f"taxonomy query must be {MAX_QUERY_CHARS} characters or fewer")
    return value


def _normalise_limit(limit: int) -> int:
    if isinstance(limit, bool):
        raise ValueError("taxonomy search limit must be an integer")
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("taxonomy search limit must be an integer") from exc
    if value < 1 or value > MAX_RESULTS:
        raise ValueError(f"taxonomy search limit must be between 1 and {MAX_RESULTS}")
    return value


def _search_taxids(query: str, limit: int) -> list[str]:
    data = _request_json(
        "esearch.fcgi",
        {
            "db": "taxonomy",
            "term": query,
            "retmode": "json",
            "retmax": str(limit),
            "sort": "relevance",
        },
    )
    ids = data.get("esearchresult", {}).get("idlist", [])
    if not isinstance(ids, list):
        return []
    return [str(item) for item in ids[:limit] if str(item).isdecimal()]


def _summarise_taxids(taxids: list[str], *, query: str) -> list[dict[str, Any]]:
    if not taxids:
        return []
    data = _request_json(
        "esummary.fcgi",
        {
            "db": "taxonomy",
            "id": ",".join(taxids[:MAX_RESULTS]),
            "retmode": "json",
        },
    )
    result = data.get("result", {})
    if not isinstance(result, dict):
        return []

    rows: list[dict[str, Any]] = []
    for taxid in taxids:
        item = result.get(taxid)
        if not isinstance(item, dict):
            continue
        rows.append(_summary_row(taxid, item, query=query))
    return rows


def _summary_row(taxid: str, item: dict[str, Any], *, query: str) -> dict[str, Any]:
    scientific_name = _safe_text(item.get("scientificname")) or _safe_text(
        item.get("scientific_name")
    )
    common_name = _safe_text(item.get("commonname")) or None
    # NB: esummary does not return `lineage` or `othernames` for db=taxonomy
    # (verified against NCBI 2026-05). We still defensively probe in case the
    # field shape ever changes; for the rich payload the caller must use
    # `fetch_taxonomy_detail(taxid)`.
    synonyms = _synonyms(item.get("othernames"))
    division = _safe_text(item.get("division")) or _safe_text(item.get("genbankdivision")) or None
    return {
        "taxid": int(taxid),
        "scientific_name": scientific_name or f"taxid {taxid}",
        "common_name": common_name,
        "rank": _safe_text(item.get("rank")) or "no rank",
        "division": division,
        "lineage": _safe_text(item.get("lineage")) or "",
        "matched_name": _matched_name(query, scientific_name, common_name, synonyms),
        "synonyms": synonyms[:10],
    }


def _synonyms(othernames: object) -> list[str]:
    if not isinstance(othernames, dict):
        return []
    values: list[str] = []
    for key in ("synonym", "equivalentname", "includes", "anamorph"):
        raw = othernames.get(key)
        if isinstance(raw, list):
            values.extend(_safe_text(item) for item in raw)
        elif raw not in (None, ""):
            values.append(_safe_text(raw))
    return [value for value in dict.fromkeys(values) if value]


def _matched_name(
    query: str,
    scientific_name: str,
    common_name: str | None,
    synonyms: list[str],
) -> str:
    lowered = query.lower()
    for candidate in [scientific_name, common_name or "", *synonyms]:
        if candidate and candidate.lower() == lowered:
            return candidate
    for candidate in [scientific_name, common_name or "", *synonyms]:
        if candidate and lowered in candidate.lower():
            return candidate
    return scientific_name


def _request_json(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    request_params = {**params, **_ncbi_identity_params()}
    # Pooled module-level client — taxonomy search is user-initiated rather
    # than polled, but pooling still saves the TLS handshake per repeated
    # lookup and matches the project-wide pattern in api/services/httpx_pool.py.
    from api.services.httpx_pool import get_pooled_client

    client = get_pooled_client(
        "taxonomy-ncbi-eutils-json",
        timeout=DEFAULT_TIMEOUT_SECONDS,
        base_url=EUTILS_BASE_URL,
        headers={"Accept": "application/json", "User-Agent": "elb-dashboard/1.0"},
    )
    try:
        response = client.get(endpoint, params=request_params)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise TaxonomySearchUnavailable("NCBI taxonomy service is unavailable") from exc
    except ValueError as exc:
        raise TaxonomySearchUnavailable("NCBI taxonomy response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise TaxonomySearchUnavailable("NCBI taxonomy response was not an object")
    return data


def _ncbi_identity_params() -> dict[str, str]:
    params: dict[str, str] = {}
    for env_name, param_name in (
        ("NCBI_TOOL", "tool"),
        ("NCBI_EMAIL", "email"),
        ("NCBI_API_KEY", "api_key"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            params[param_name] = value
    return params


def _safe_text(value: object) -> str:
    return str(value or "").strip()[:500]


# ---------------------------------------------------------------------------
# Detail (efetch XML) — lazy, single-taxid lookup that fills in lineage,
# synonyms, parent_taxid, genetic codes, division, dates.
# ---------------------------------------------------------------------------
