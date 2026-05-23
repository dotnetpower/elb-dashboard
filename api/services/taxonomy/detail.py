"""NCBI taxonomy detail (efetch) + XML parsing + _request_bytes."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from api.services.taxonomy.cache import (
    _detail_cache_get,
    _detail_cache_put,
)
from api.services.taxonomy.search import (
    DEFAULT_TIMEOUT_SECONDS,
    EUTILS_BASE_URL,
    TaxonomySearchUnavailable,
    _ncbi_identity_params,
)

LOGGER = logging.getLogger(__name__)

MAX_EFETCH_BYTES = 512 * 1024


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
        taxid: int = int(value)  # type: ignore[call-overload]
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
    mito_genetic_code = _xml_text(_find_subelement(taxon, "MitoGeneticCode", "MGCName")) or None
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
    empty: dict[str, Any] = {
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


def _request_bytes(endpoint: str, params: dict[str, str], *, max_bytes: int) -> bytes:
    """Fetch a binary payload from NCBI eutils with a hard byte cap."""
    request_params = {**params, **_ncbi_identity_params()}
    from api.services.httpx_pool import get_pooled_client

    client = get_pooled_client(
        "taxonomy-ncbi-eutils-xml",
        timeout=DEFAULT_TIMEOUT_SECONDS,
        base_url=EUTILS_BASE_URL,
        headers={"Accept": "application/xml", "User-Agent": "elb-dashboard/1.0"},
    )
    try:
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
