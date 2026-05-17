"""NCBI taxonomy search helpers for BLAST submit filters.

Two-stage design (cheap list -> rich detail) avoids the per-row XML fetch
cost. `search_taxonomy` only calls esummary (small JSON), so `lineage`,
`synonyms`, `parent_taxid`, `genetic_code` are intentionally left empty
in the list payload — the dashboard pulls them lazily via
`fetch_taxonomy_detail(taxid)` (efetch XML) when the user actually
selects a candidate. XML parsing goes through `defusedxml` so that the
NCBI response cannot trigger XXE / billion-laughs against the api
sidecar.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

LOGGER = logging.getLogger(__name__)

EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_QUERY_CHARS = 120
MAX_RESULTS = 20
# Defensive caps for upstream payloads. NCBI esummary for one taxon is
# typically <2 KB; efetch XML for a single taxon (with LineageEx) is
# typically <30 KB. We cap well above the p99 to absorb deep lineages
# without leaving the api sidecar exposed to a runaway response.
MAX_ESUMMARY_BYTES = 256 * 1024
MAX_EFETCH_BYTES = 512 * 1024
MAX_DETAIL_CACHE_ENTRIES = 1024

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

_DETAIL_CACHE_LOCK = threading.Lock()
_DETAIL_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


class TaxonomySearchUnavailable(RuntimeError):
    """Raised when NCBI taxonomy lookup cannot be completed."""


def clear_taxonomy_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


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


def _cache_get(cache_key: tuple[str, int]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(cache_key)
        if item is None:
            return None
        expires_at, payload = item
        if expires_at <= now:
            _CACHE.pop(cache_key, None)
            return None
        return dict(payload)


def _cache_put(cache_key: tuple[str, int], payload: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + DEFAULT_CACHE_TTL_SECONDS, dict(payload))


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
    try:
        with httpx.Client(
            base_url=EUTILS_BASE_URL,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={"Accept": "application/json", "User-Agent": "elb-dashboard/1.0"},
        ) as client:
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


def fetch_taxonomy_detail(taxid: int) -> dict[str, Any]:
    """Fetch the rich detail payload for a single NCBI taxid (cached)."""
    normalised = _normalise_taxid(taxid)
    cached = _detail_cache_get(normalised)
    if cached is not None:
        return {**cached, "cached": True}
    try:
        body = _request_bytes(
            "efetch.fcgi",
            {"db": "taxonomy", "id": str(normalised), "retmode": "xml"},
            max_bytes=MAX_EFETCH_BYTES,
        )
    except TaxonomySearchUnavailable:
        raise
    payload = _parse_taxonomy_xml(body, expected_taxid=normalised)
    payload["cached"] = False
    _detail_cache_put(normalised, payload)
    return payload


def _normalise_taxid(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("taxonomy taxid must be a positive integer")
    try:
        taxid = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("taxonomy taxid must be a positive integer") from exc
    # NCBI taxids stay well under 1e10. Cap defensively so a hostile caller
    # cannot push arbitrarily large integers into upstream URLs.
    if taxid <= 0 or taxid > 10**10:
        raise ValueError("taxonomy taxid must be a positive integer")
    return taxid


def _parse_taxonomy_xml(body: bytes, *, expected_taxid: int) -> dict[str, Any]:
    """Parse an efetch TaxaSet XML response into a flat dict.

    Uses `defusedxml` so external entities / billion-laughs cannot harm
    the api sidecar even if the upstream is compromised.
    """
    try:
        root = DefusedET.fromstring(body)
    except DefusedET.ParseError as exc:
        LOGGER.warning("taxonomy efetch XML unparseable (taxid=%s): %s", expected_taxid, exc)
        raise TaxonomySearchUnavailable("NCBI taxonomy XML was not parseable") from exc
    except DefusedXmlException as exc:
        LOGGER.warning(
            "taxonomy efetch XML rejected by safe parser (taxid=%s): %s",
            expected_taxid,
            exc.__class__.__name__,
        )
        raise TaxonomySearchUnavailable("NCBI taxonomy XML rejected by safe parser") from exc

    taxon = root.find("Taxon") if root.tag == "TaxaSet" else root
    if taxon is None or taxon.tag != "Taxon":
        raise TaxonomySearchUnavailable("NCBI taxonomy response did not contain a Taxon")

    taxid_text = _xml_text(taxon.find("TaxId"))
    try:
        taxid = int(taxid_text)
    except (TypeError, ValueError) as exc:
        raise TaxonomySearchUnavailable("NCBI taxonomy response missing TaxId") from exc
    if taxid != expected_taxid:
        # Upstream returned a different taxon (e.g. merged/deleted node).
        # Keep the response but surface the actual taxid so the caller can
        # decide what to render.
        pass

    scientific_name = _xml_text(taxon.find("ScientificName"))
    rank = _xml_text(taxon.find("Rank")) or "no rank"
    division = _xml_text(taxon.find("Division")) or None
    parent_taxid_text = _xml_text(taxon.find("ParentTaxId"))
    parent_taxid: int | None
    try:
        parent_taxid = int(parent_taxid_text) if parent_taxid_text else None
    except ValueError:
        parent_taxid = None

    other = _parse_other_names(taxon.find("OtherNames"))
    lineage_str = _xml_text(taxon.find("Lineage")) or ""
    lineage_ex = _parse_lineage_ex(taxon.find("LineageEx"))

    genetic_code = _xml_text(_find_subelement(taxon, "GeneticCode", "GCName")) or None
    genetic_code_id_text = _xml_text(_find_subelement(taxon, "GeneticCode", "GCId"))
    mito_genetic_code = (
        _xml_text(_find_subelement(taxon, "MitoGeneticCode", "MGCName")) or None
    )
    mito_genetic_code_id_text = _xml_text(_find_subelement(taxon, "MitoGeneticCode", "MGCId"))

    create_date = _xml_text(taxon.find("CreateDate")) or None
    update_date = _xml_text(taxon.find("UpdateDate")) or None
    pub_date = _xml_text(taxon.find("PubDate")) or None

    return {
        "taxid": taxid,
        "scientific_name": _truncate(scientific_name) or f"taxid {taxid}",
        "common_name": other["common_name"],
        "rank": _truncate(rank),
        "division": _truncate(division) if division else None,
        "parent_taxid": parent_taxid,
        "authority": other["authority"],
        "synonyms": other["synonyms"][:20],
        "equivalent_names": other["equivalent_names"][:10],
        "misspellings": other["misspellings"][:10],
        "lineage": _truncate(lineage_str, limit=2000),
        "lineage_ex": lineage_ex[:64],
        "genetic_code": genetic_code,
        "genetic_code_id": _safe_int(genetic_code_id_text),
        "mito_genetic_code": mito_genetic_code,
        "mito_genetic_code_id": _safe_int(mito_genetic_code_id_text),
        "create_date": _truncate(create_date) if create_date else None,
        "update_date": _truncate(update_date) if update_date else None,
        "pub_date": _truncate(pub_date) if pub_date else None,
        "source": "ncbi_eutils",
    }


def _find_subelement(parent: Any, container: str, leaf: str) -> Any:
    node = parent.find(container)
    if node is None:
        return None
    return node.find(leaf)


def _parse_other_names(node: Any) -> dict[str, Any]:
    empty = {
        "common_name": None,
        "authority": None,
        "synonyms": [],
        "equivalent_names": [],
        "misspellings": [],
    }
    if node is None:
        return empty

    common_name: str | None = None
    authority: str | None = None
    synonyms: list[str] = []
    equivalent_names: list[str] = []
    misspellings: list[str] = []

    # Direct child tags (GenbankCommonName, CommonName, Synonym, EquivalentName)
    for child in list(node):
        tag = child.tag
        text = _truncate(_xml_text(child))
        if not text:
            continue
        if tag in ("GenbankCommonName", "CommonName") and common_name is None:
            common_name = text
        elif tag == "Synonym":
            synonyms.append(text)
        elif tag == "EquivalentName":
            equivalent_names.append(text)

    # <Name><ClassCDE>authority|misspelling|...</ClassCDE><DispName>...</DispName></Name>
    for name_node in node.findall("Name"):
        class_cde = _xml_text(name_node.find("ClassCDE")).lower()
        disp_name = _truncate(_xml_text(name_node.find("DispName")))
        if not disp_name:
            continue
        if class_cde == "authority" and authority is None:
            authority = disp_name
        elif class_cde == "misspelling":
            misspellings.append(disp_name)
        elif class_cde in ("synonym", "blast name"):
            synonyms.append(disp_name)
        elif class_cde == "equivalent name":
            equivalent_names.append(disp_name)
        elif class_cde in ("genbank common name", "common name") and common_name is None:
            common_name = disp_name

    return {
        "common_name": common_name,
        "authority": authority,
        "synonyms": _dedupe(synonyms),
        "equivalent_names": _dedupe(equivalent_names),
        "misspellings": _dedupe(misspellings),
    }


def _parse_lineage_ex(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    rows: list[dict[str, Any]] = []
    for taxon_node in node.findall("Taxon"):
        taxid_text = _xml_text(taxon_node.find("TaxId"))
        try:
            taxid = int(taxid_text)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "taxid": taxid,
                "scientific_name": _truncate(_xml_text(taxon_node.find("ScientificName")))
                or f"taxid {taxid}",
                "rank": _truncate(_xml_text(taxon_node.find("Rank"))) or "no rank",
            }
        )
    return rows


def _xml_text(node: Any) -> str:
    if node is None:
        return ""
    text = (node.text or "").strip()
    return text


def _truncate(value: str | None, *, limit: int = 500) -> str:
    if value is None:
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "\u2026"


def _safe_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _dedupe(values: list[str]) -> list[str]:
    return [item for item in dict.fromkeys(values) if item]


def _detail_cache_get(taxid: int) -> dict[str, Any] | None:
    with _DETAIL_CACHE_LOCK:
        entry = _DETAIL_CACHE.get(taxid)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            _DETAIL_CACHE.pop(taxid, None)
            return None
        return dict(payload)


def _detail_cache_put(taxid: int, payload: dict[str, Any]) -> None:
    with _DETAIL_CACHE_LOCK:
        if len(_DETAIL_CACHE) >= MAX_DETAIL_CACHE_ENTRIES:
            # Drop the oldest entry (insertion order). Cheap LRU-ish eviction
            # without an extra dependency.
            try:
                oldest_key = next(iter(_DETAIL_CACHE))
                _DETAIL_CACHE.pop(oldest_key, None)
            except StopIteration:
                pass
        _DETAIL_CACHE[taxid] = (
            time.monotonic() + DEFAULT_CACHE_TTL_SECONDS,
            dict(payload),
        )


def clear_taxonomy_detail_cache() -> None:
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE.clear()


# ---------------------------------------------------------------------------
# Siblings / tree — fetch direct taxonomic siblings at each major rank
# for the cladogram visualisation.  Caches per (parent_taxid, rank) pair.
# ---------------------------------------------------------------------------

_MAJOR_RANKS_SET = frozenset([
    "superkingdom", "kingdom", "phylum", "class",
    "order", "family", "genus", "species",
])

_SIBLINGS_CACHE_LOCK = threading.Lock()
_SIBLINGS_CACHE: dict[tuple[int, str, int], tuple[float, list[dict[str, Any]]]] = {}
MAX_SIBLINGS_CACHE_ENTRIES = 512


def _siblings_cache_get(
    key: tuple[int, str, int],
) -> list[dict[str, Any]] | None:
    with _SIBLINGS_CACHE_LOCK:
        entry = _SIBLINGS_CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            _SIBLINGS_CACHE.pop(key, None)
            return None
        return list(payload)


def _siblings_cache_put(
    key: tuple[int, str, int],
    payload: list[dict[str, Any]],
) -> None:
    with _SIBLINGS_CACHE_LOCK:
        if len(_SIBLINGS_CACHE) >= MAX_SIBLINGS_CACHE_ENTRIES:
            try:
                oldest = next(iter(_SIBLINGS_CACHE))
                _SIBLINGS_CACHE.pop(oldest, None)
            except StopIteration:
                pass
        _SIBLINGS_CACHE[key] = (
            time.monotonic() + DEFAULT_CACHE_TTL_SECONDS,
            list(payload),
        )


def clear_taxonomy_siblings_cache() -> None:
    with _SIBLINGS_CACHE_LOCK:
        _SIBLINGS_CACHE.clear()


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
        taxids = _search_taxids(term, min(safe_limit + 5, MAX_RESULTS))
        rows = []
        if taxids:
            summaries = _summarise_taxids(taxids, query="")
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
    taxid: int, *, sibling_limit: int = 3,
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
    lineage = lineage_ex + [
        {
            "taxid": detail["taxid"],
            "scientific_name": detail["scientific_name"],
            "rank": detail["rank"],
        }
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


def _request_bytes(endpoint: str, params: dict[str, str], *, max_bytes: int) -> bytes:
    """Fetch a binary payload from NCBI eutils with a hard byte cap."""
    request_params = {**params, **_ncbi_identity_params()}
    try:
        with httpx.Client(
            base_url=EUTILS_BASE_URL,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={"Accept": "application/xml", "User-Agent": "elb-dashboard/1.0"},
        ) as client:
            with client.stream("GET", endpoint, params=request_params) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise TaxonomySearchUnavailable(
                            "NCBI taxonomy response exceeded size limit"
                        )
                return bytes(buffer)
    except httpx.HTTPError as exc:
        raise TaxonomySearchUnavailable("NCBI taxonomy service is unavailable") from exc
