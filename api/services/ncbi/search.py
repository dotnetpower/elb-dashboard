"""NCBI nuccore search (esearch + esummary) and feature-table parsing.

Responsibility: Back the New Search "Generate query" modal — turn an
organism/keyword term into a short list of candidate nucleotide records
(``search_nuccore``) and turn a chosen accession into its gene/CDS feature
list with coordinates (``fetch_feature_table``) so a researcher can pick a
sub-range the way they would on the NCBI BLAST web form.
Edit boundaries: Nucleotide (``db=nuccore``) discovery only. The actual FASTA
fetch stays in ``nuccore.fetch_nuccore_fasta``; shared HTTP / identity /
rate-limit primitives stay in ``_eutils``. No HTTP route shaping here.
Key entry points: ``search_nuccore``, ``fetch_feature_table``,
``NcbiServiceUnavailable`` (re-exported for the route layer).
Risky contracts: Every NCBI call goes through ``_eutils.request_*`` so the
shared token bucket and ``ncbi_identity_params`` (API key) apply. The esearch
``term`` is length-capped and stripped of control characters but otherwise
forwarded verbatim because it is NCBI's Entrez query language, not a shell/SQL
context; httpx URL-encodes it. Feature parsing is byte-capped and feature-count
capped so a pathological record cannot balloon the api sidecar.
Validation: ``uv run pytest -q api/tests/test_ncbi_search.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from api.services.ncbi._eutils import (
    NcbiServiceUnavailable,
    request_bytes,
    request_json,
)
from api.services.ncbi.nuccore import normalise_accession

LOGGER = logging.getLogger(__name__)

# esearch term guard. Long enough for a realistic Entrez query
# (``"monkeypox virus"[Organism] AND complete genome``) but bounded so a
# hostile caller cannot push a multi-kilobyte string into the upstream URL.
MAX_TERM_CHARS = 200
# Result fan-out caps. The modal shows a short candidate list; a researcher
# narrows the term rather than paging hundreds of hits.
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 25
# Feature table can be ~2 MB for a bacterial genome (~5k genes); cap at 6 MiB so
# the common bacterial/viral/organelle records parse, while a chromosome-scale
# assembly (tens of MB of features) is still rejected rather than streamed into
# memory. The caller then falls back to a manual sub-range.
MAX_FEATURE_TABLE_BYTES = 6 * 1024 * 1024
# A poxvirus genome carries ~200 genes; 1000 is a generous ceiling that still
# bounds the response the SPA has to render.
MAX_FEATURES_RETURNED = 1000


def search_nuccore(term: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
    """Search ``db=nuccore`` by free-text term and return candidate records.

    Two NCBI calls: ``esearch`` to resolve the term to a list of UIDs, then a
    single batched ``esummary`` for the lightweight header fields the modal
    needs (accession, title, organism, length, molecule type, RefSeq flag).
    """
    normalised_term = _normalise_term(term)
    normalised_limit = _normalise_limit(limit)
    try:
        uids = _esearch_uids(normalised_term, normalised_limit)
        results = _esummary_rows(uids) if uids else []
    except NcbiServiceUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - defensive net
        raise NcbiServiceUnavailable("NCBI nuccore search failed") from exc
    return {
        "query": normalised_term,
        "count": len(results),
        "source": "ncbi_eutils",
        "results": results,
    }


def fetch_feature_table(accession: str, *, limit: int = MAX_FEATURES_RETURNED) -> dict[str, Any]:
    """Return the gene/CDS features (name + 1-based coordinates) for a record.

    Uses ``efetch rettype=ft`` (the lightweight tab-delimited feature table)
    rather than full GenBank XML. Each gene feature is paired with the product
    of the CDS that shares its coordinates so the researcher sees a meaningful
    label, and ``strand`` is ``minus`` when NCBI lists the feature as
    ``start > stop`` (the FASTA fetch reproduces that with ``seq_start >
    seq_stop``).
    """
    normalised = normalise_accession(accession)
    capped_limit = max(1, min(int(limit), MAX_FEATURES_RETURNED))
    body = request_bytes(
        "efetch.fcgi",
        {"db": "nuccore", "id": normalised, "rettype": "ft", "retmode": "text"},
        max_bytes=MAX_FEATURE_TABLE_BYTES,
        accept="text/plain",
    )
    text = body.decode("utf-8", errors="replace")
    features = _parse_feature_table(text, limit=capped_limit)
    return {
        "accession": normalised.split(".")[0],
        "accession_version": normalised,
        "count": len(features),
        "source": "ncbi_eutils",
        "features": features,
    }


# ---------------------------------------------------------------------------
# esearch / esummary
# ---------------------------------------------------------------------------
def _esearch_uids(term: str, limit: int) -> list[str]:
    data = request_json(
        "esearch.fcgi",
        {
            "db": "nuccore",
            "term": term,
            "retmode": "json",
            "retmax": str(limit),
            "sort": "relevance",
        },
    )
    result = data.get("esearchresult")
    if not isinstance(result, dict):
        return []
    ids = result.get("idlist")
    if not isinstance(ids, list):
        return []
    return [str(item) for item in ids[:limit] if str(item).isdecimal()]


def _esummary_rows(uids: list[str]) -> list[dict[str, Any]]:
    data = request_json(
        "esummary.fcgi",
        {"db": "nuccore", "id": ",".join(uids), "retmode": "json"},
    )
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    rows: list[dict[str, Any]] = []
    # Preserve esearch relevance order via the ``uids`` list rather than dict
    # iteration order.
    order = result.get("uids") if isinstance(result.get("uids"), list) else uids
    for uid in order:
        record = result.get(str(uid))
        if isinstance(record, dict):
            rows.append(_summary_row(record))
    return rows


def _summary_row(record: dict[str, Any]) -> dict[str, Any]:
    accession_version = _str(record.get("accessionversion"))
    source_db = _str(record.get("sourcedb")).lower() or None
    slen = _int(record.get("slen"))
    return {
        "accession": accession_version.split(".")[0] if accession_version else "",
        "accession_version": accession_version,
        "title": _truncate(_str(record.get("title"))),
        "organism": _truncate(_str(record.get("organism")), 200),
        "taxid": _int(record.get("taxid")),
        "length": slen,
        "moltype": _str(record.get("moltype")).lower() or None,
        "biomol": _str(record.get("biomol")).lower() or None,
        "is_refseq": source_db == "refseq",
        "source_db": source_db,
        "status": _str(record.get("status")).lower() or None,
    }


# ---------------------------------------------------------------------------
# Feature table (rettype=ft) parsing
# ---------------------------------------------------------------------------
def _parse_feature_table(text: str, *, limit: int) -> list[dict[str, Any]]:
    """Parse a tab-delimited NCBI feature table into gene/CDS rows.

    Format (tabs shown as ``\\t``)::

        >Feature ref|NC_063383.1|
        1575\\t835\\tgene
        \\t\\t\\tgene\\tOPG001
        \\t\\t\\tlocus_tag\\tNBT03_gp001
        1575\\t835\\tCDS
        \\t\\t\\tproduct\\tphospholipase

    A feature begins on a line whose first two tab fields are integers and
    whose third field is the feature key. Following indented lines (empty
    leading fields) carry ``\\tqualifier\\tvalue`` pairs. We keep ``gene`` and
    ``CDS`` features; CDS ``product`` values are merged onto the gene that
    shares the same coordinates.
    """
    genes: list[dict[str, Any]] = []
    # Coordinate-keyed index so a CDS can attach its product to the matching
    # gene without an O(n^2) rescan.
    by_span: dict[tuple[int, int], dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_key: str | None = None

    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith(">Feature"):
            continue
        fields = raw_line.split("\t")
        start_raw, stop_raw = fields[0], fields[1] if len(fields) > 1 else ""
        if start_raw and stop_raw and _looks_int(start_raw) and _looks_int(stop_raw):
            # New feature header line.
            key = fields[2].strip() if len(fields) > 2 else ""
            current_key = key
            if key not in ("gene", "CDS"):
                current = None
                continue
            start = int(start_raw)
            stop = int(stop_raw)
            span = (start, stop)
            if key == "gene":
                if len(genes) >= limit:
                    current = None
                    continue
                low, high = (stop, start) if start > stop else (start, stop)
                current = {
                    "type": "gene",
                    "name": None,
                    "product": None,
                    "locus_tag": None,
                    "start": low,
                    "stop": high,
                    "strand": "minus" if start > stop else "plus",
                    "length": high - low + 1,
                }
                genes.append(current)
                by_span[span] = current
            else:  # CDS — attach to the gene sharing this span if present.
                current = by_span.get(span)
            continue
        # Qualifier line: ``\t\t\tqualifier\tvalue`` → trailing two fields.
        if current is None or len(fields) < 5:
            continue
        qualifier = fields[3].strip()
        value = fields[4].strip()
        if not qualifier or not value:
            continue
        if current_key == "gene" and qualifier in ("gene", "locus_tag"):
            target = "name" if qualifier == "gene" else "locus_tag"
            if current.get(target) is None:
                current[target] = value[:120]
        elif current_key == "CDS" and qualifier == "product":
            if current.get("product") is None:
                current["product"] = value[:200]

    return genes


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _normalise_term(term: str) -> str:
    value = " ".join(str(term or "").replace("\x00", " ").split())
    if not value:
        raise ValueError("search term is required")
    if len(value) > MAX_TERM_CHARS:
        raise ValueError(f"search term must be {MAX_TERM_CHARS} characters or fewer")
    return value


def _normalise_limit(limit: int) -> int:
    if isinstance(limit, bool):
        raise ValueError("search limit must be an integer")
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("search limit must be an integer") from exc
    if value < 1 or value > MAX_SEARCH_LIMIT:
        raise ValueError(f"search limit must be between 1 and {MAX_SEARCH_LIMIT}")
    return value


def _looks_int(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    return stripped.lstrip("-").isdecimal()


def _str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truncate(value: str, limit: int = 300) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"
