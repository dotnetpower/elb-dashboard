"""NCBI nuccore (esummary / efetch GenBank / efetch FASTA) fetch + parse.

Responsibility: For a single NCBI nucleotide accession, return three views —
``summary`` (esummary JSON header), ``genbank`` (efetch GBSet XML parsed into a
flat record + features list), and ``fasta`` (efetch FASTA text, optionally
subranged). Includes process-wide TTL caches keyed by ``accession.version`` (+
subrange for FASTA) so repeated dashboard polls stay within NCBI policy.
A second, durable cross-sidecar cache (ops Redis, JSON, 7-day TTL) backs the
``summary`` and ``genbank`` views so the first viewer on a cold api replica /
worker — and the first viewer after an api sidecar restart — reuses a payload
another sidecar already fetched instead of paying the 10-16 s efetch again.
Edit boundaries: Strictly nucleotide records (``db=nuccore``). Protein /
taxonomy / gene / pubmed live in their own modules. Shared HTTP +
identity + rate-limit primitives stay in ``_eutils.py``.
Key entry points: `normalise_accession`, `fetch_nuccore_summary`,
`fetch_nuccore_genbank`, `fetch_nuccore_fasta`, `clear_nuccore_caches`.
Risky contracts: ``normalise_accession`` rejects anything that does not match
the conservative NCBI accession pattern. All XML parsing goes through
``defusedxml`` to neutralise XXE / billion-laughs. FASTA bodies bigger than
``MAX_FASTA_BYTES`` are rejected — the caller must pass an explicit subrange to
exercise large records. The durable cache is best-effort: any Redis error (or
``NCBI_DURABLE_CACHE_DISABLED=true``) degrades silently to the in-process cache
+ live NCBI fetch, so it can never change correctness, only latency.
Validation: `uv run pytest -q api/tests/test_ncbi_nuccore.py`.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from api.services.ncbi._eutils import (
    NcbiServiceUnavailable,
    request_bytes,
    request_json,
)

LOGGER = logging.getLogger(__name__)

# Byte caps. esummary is tiny; GenBank XML for a single locus can balloon when
# the record has long REFERENCE / COMMENT blocks; FASTA scales with sequence
# length so we cap at ~5 MiB which covers the vast majority of single-gene /
# rRNA / virus records but rejects chromosome-scale assemblies.
MAX_SUMMARY_BYTES = 256 * 1024
MAX_GENBANK_BYTES = 2 * 1024 * 1024
MAX_FASTA_BYTES = 5 * 1024 * 1024
MAX_FEATURES_PER_RECORD = 2000
MAX_DESCRIPTION_CHARS = 2000

# Conservative accession pattern. Accepts modern RefSeq prefixes
# (NM_/NR_/XM_/XR_/NC_/AC_/NT_/NW_/NZ_/XP_/NP_), GenBank-style 1/2/3-letter
# prefixes + digits, and an optional ``.version`` suffix. Anything else is
# rejected so we never forward arbitrary strings to NCBI.
_ACCESSION_RE = re.compile(r"^[A-Z]{1,4}_?[0-9]+(?:\.[0-9]{1,3})?$")
_MAX_ACCESSION_LENGTH = 32

_CACHE_TTL_SECONDS = 24 * 60 * 60
_MAX_CACHE_ENTRIES = 512

# Durable cross-sidecar cache (ops Redis). The in-process caches above are
# per-replica and reset on every api/worker restart, so the first viewer on a
# cold replica still pays the 10-16 s efetch for a large record (issue #27).
# A shared Redis layer lets any sidecar reuse a payload another already
# fetched. Best-effort only: any Redis error (or the kill-switch env var)
# degrades silently to the in-process cache + live NCBI fetch, so it can only
# change latency, never correctness. Keyed by view + accession.version; values
# are JSON (the parsed payload dict). 7-day TTL because GenBank records for a
# fixed accession.version are immutable once published.
_DURABLE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_DURABLE_CACHE_KEY_PREFIX = "ncbi:nuccore:"
# Skip persisting payloads larger than this to keep ops Redis lean; the
# in-process cache still serves them within a replica.
_DURABLE_CACHE_MAX_VALUE_BYTES = 1 * 1024 * 1024


def normalise_accession(accession: str) -> str:
    """Validate + normalise an NCBI accession.

    Strips any ``gi|...|ref|NM_001.1|`` prefix and returns the bare
    ``accession.version`` (or ``accession``) form, uppercased.
    """
    if not isinstance(accession, str):
        raise ValueError("accession must be a string")
    raw = accession.strip()
    if not raw:
        raise ValueError("accession is required")
    if "|" in raw:
        # FASTA-style identifier (``gi|XYZ|ref|NM_001.1|name``). Take the
        # final pipe-delimited element that looks like an accession.
        parts = [p for p in raw.split("|") if p]
        for candidate in reversed(parts):
            normalised = candidate.strip()
            if _ACCESSION_RE.match(normalised.upper()):
                raw = normalised
                break
        else:
            raise ValueError("accession does not contain a recognisable identifier")
    if len(raw) > _MAX_ACCESSION_LENGTH:
        raise ValueError(f"accession exceeds {_MAX_ACCESSION_LENGTH} characters")
    upper = raw.upper()
    if not _ACCESSION_RE.match(upper):
        raise ValueError("accession is not a recognisable NCBI identifier")
    return upper


# ---------------------------------------------------------------------------
# TTL cache primitives
#
# The buckets are ``OrderedDict`` so we can give them LRU semantics (hot keys
# get moved to the end on read and are last to be evicted). Payloads are
# stored as deep copies in both directions so callers can mutate the result
# without polluting the cache, and a subsequent reader cannot observe a
# previous reader's edits.
# ---------------------------------------------------------------------------
_SUMMARY_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_GENBANK_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_FASTA_CACHE: (
    OrderedDict[tuple[str, int | None, int | None], tuple[float, str]]
) = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_get(
    bucket: OrderedDict[Any, tuple[float, Any]], key: Any
) -> Any | None:
    # Two-phase read: hold the lock only long enough to fetch the reference
    # and update LRU ordering, then release before the (potentially large)
    # deep copy so other readers are not blocked on a ~100 KiB GenBank dict
    # being duplicated. The cached payload is treated as immutable while we
    # hold the reference; ``_cache_put`` always writes a fresh tuple.
    with _CACHE_LOCK:
        entry = bucket.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            bucket.pop(key, None)
            return None
        # Mark the entry as most-recently-used so LRU eviction skips it.
        bucket.move_to_end(key)
    # Deep copy outside the lock so callers cannot mutate cached nested
    # lists/dicts (features, references, intervals, …) and surprise the
    # next reader, without serialising every reader on the duplicate.
    return copy.deepcopy(payload)


def _cache_put(
    bucket: OrderedDict[Any, tuple[float, Any]], key: Any, payload: Any
) -> None:
    # Deep copy outside the lock so a large payload does not block other
    # writers/readers during duplication. The lock then only covers the
    # actual dict mutation (eviction + insert).
    snapshot = copy.deepcopy(payload)
    expires_at = time.monotonic() + _CACHE_TTL_SECONDS
    with _CACHE_LOCK:
        if key in bucket:
            bucket.pop(key, None)
        elif len(bucket) >= _MAX_CACHE_ENTRIES:
            # LRU eviction: pop the least-recently-used entry (head of dict).
            try:
                bucket.popitem(last=False)
            except KeyError:
                pass
        bucket[key] = (expires_at, snapshot)


def clear_nuccore_caches() -> None:
    with _CACHE_LOCK:
        _SUMMARY_CACHE.clear()
        _GENBANK_CACHE.clear()
        _FASTA_CACHE.clear()


# ---------------------------------------------------------------------------
# Durable cross-sidecar cache (ops Redis)
#
# Best-effort JSON cache shared by every api/worker sidecar. All helpers
# swallow every error: a miss, a Redis outage, or the kill-switch env var all
# return ``None`` (read) or no-op (write) so the caller falls through to the
# in-process cache + live NCBI fetch. This layer can only improve latency,
# never correctness.
# ---------------------------------------------------------------------------
def _durable_cache_enabled() -> bool:
    return os.environ.get("NCBI_DURABLE_CACHE_DISABLED", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )


def _durable_cache_client() -> Any | None:
    if not _durable_cache_enabled():
        return None
    try:
        from api.services.redis_clients import get_ops_redis_client

        return get_ops_redis_client(socket_timeout=0.5)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("nuccore durable cache redis unavailable: %s", type(exc).__name__)
        return None


def _durable_cache_key(view: str, normalised: str) -> str:
    return f"{_DURABLE_CACHE_KEY_PREFIX}{view}:{normalised}"


def _durable_cache_get(view: str, normalised: str) -> dict[str, Any] | None:
    client = _durable_cache_client()
    if client is None:
        return None
    try:
        raw = client.get(_durable_cache_key(view, normalised))
    except Exception as exc:
        LOGGER.debug("nuccore durable cache get failed: %s", type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        body = json.loads(raw)
        if isinstance(body, dict):
            return body
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return None


def _durable_cache_set(view: str, normalised: str, payload: dict[str, Any]) -> None:
    client = _durable_cache_client()
    if client is None:
        return
    try:
        serialised = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        LOGGER.debug("nuccore durable cache serialise failed: %s", type(exc).__name__)
        return
    if len(serialised.encode("utf-8")) > _DURABLE_CACHE_MAX_VALUE_BYTES:
        # Oversized record: skip the shared cache; the in-process LRU still
        # serves it within this replica.
        return
    try:
        client.setex(
            _durable_cache_key(view, normalised),
            _DURABLE_CACHE_TTL_SECONDS,
            serialised,
        )
    except Exception as exc:
        LOGGER.debug("nuccore durable cache setex failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# esummary
# ---------------------------------------------------------------------------
def fetch_nuccore_summary(accession: str) -> dict[str, Any]:
    """Return the parsed esummary JSON for a single accession (cached)."""
    normalised = normalise_accession(accession)
    cached = _cache_get(_SUMMARY_CACHE, normalised)
    if cached is not None:
        return {**cached, "cached": True}
    durable = _durable_cache_get("summary", normalised)
    if durable is not None:
        # Re-seed the in-process cache so subsequent reads on this replica
        # skip Redis entirely.
        _cache_put(_SUMMARY_CACHE, normalised, durable)
        return {**durable, "cached": True}
    data = request_json(
        "esummary.fcgi",
        {"db": "nuccore", "id": normalised, "retmode": "json"},
    )
    payload = _parse_esummary(data, normalised)
    _cache_put(_SUMMARY_CACHE, normalised, payload)
    _durable_cache_set("summary", normalised, payload)
    return {**payload, "cached": False}


def _parse_esummary(data: dict[str, Any], normalised: str) -> dict[str, Any]:
    result = data.get("result")
    if not isinstance(result, dict):
        raise NcbiServiceUnavailable("NCBI esummary response missing `result`")
    uids = result.get("uids") if isinstance(result.get("uids"), list) else []
    record: dict[str, Any] | None = None
    for uid in uids:
        candidate = result.get(str(uid))
        if isinstance(candidate, dict):
            record = candidate
            break
    if record is None:
        # esummary may return ``error`` for invalid ids.
        err = result.get("error") or data.get("error")
        if err:
            raise NcbiServiceUnavailable(f"NCBI esummary error: {str(err)[:200]}")
        raise NcbiServiceUnavailable("NCBI esummary returned no records")

    accession_version = _str(record.get("accessionversion")) or normalised
    title = _truncate(_str(record.get("title")) or "")
    organism = _truncate(_str(record.get("organism")) or "")
    taxid = _int(record.get("taxid"))
    slen = _int(record.get("slen"))
    moltype = _str(record.get("moltype")).lower() or None
    biomol = _str(record.get("biomol")).lower() or None
    completeness = _str(record.get("completeness")).lower() or None
    sourcedb = _str(record.get("sourcedb")) or None
    strand = _str(record.get("strand")).lower() or None
    topology = _str(record.get("topology")).lower() or None
    createdate = _str(record.get("createdate")) or None
    updatedate = _str(record.get("updatedate")) or None
    # Record-status trust signals. NCBI esummary surfaces ``status``
    # ("live" / "replaced" / "suppressed" / "withdrawn" / "dead") and, when a
    # newer accession supersedes this one, ``replacedby``. A molecular-
    # diagnostics user checks these before trusting a sequence, so we expose
    # them verbatim (lowercased status, bare replacement accession).
    status = _str(record.get("status")).lower() or None
    replaced_by = _str(record.get("replacedby")) or None

    return {
        "accession": accession_version.split(".")[0],
        "accession_version": accession_version,
        "title": title,
        "organism": organism,
        "taxid": taxid,
        "length": slen,
        "moltype": moltype,
        "biomol": biomol,
        "completeness": completeness,
        "source_db": sourcedb,
        "strand": strand,
        "topology": topology,
        "create_date": createdate,
        "update_date": updatedate,
        "status": status,
        "replaced_by": replaced_by,
        "source": "ncbi_eutils",
    }


# ---------------------------------------------------------------------------
# efetch GenBank XML
# ---------------------------------------------------------------------------
def fetch_nuccore_genbank(accession: str) -> dict[str, Any]:
    """Return a parsed GBSet record for a single accession (cached)."""
    normalised = normalise_accession(accession)
    cached = _cache_get(_GENBANK_CACHE, normalised)
    if cached is not None:
        return {**cached, "cached": True}
    durable = _durable_cache_get("genbank", normalised)
    if durable is not None:
        _cache_put(_GENBANK_CACHE, normalised, durable)
        return {**durable, "cached": True}
    body = request_bytes(
        "efetch.fcgi",
        {
            "db": "nuccore",
            "id": normalised,
            "rettype": "gb",
            "retmode": "xml",
        },
        max_bytes=MAX_GENBANK_BYTES,
        accept="application/xml",
    )
    payload = _parse_genbank_xml(body, normalised)
    _cache_put(_GENBANK_CACHE, normalised, payload)
    _durable_cache_set("genbank", normalised, payload)
    return {**payload, "cached": False}


def _parse_genbank_xml(body: bytes, normalised: str) -> dict[str, Any]:
    try:
        root = DefusedET.fromstring(body)
    except DefusedET.ParseError as exc:
        LOGGER.warning("nuccore genbank XML unparseable (acc=%s): %s", normalised, exc)
        raise NcbiServiceUnavailable("NCBI GenBank XML was not parseable") from exc
    except DefusedXmlException as exc:
        LOGGER.warning(
            "nuccore genbank XML rejected by safe parser (acc=%s): %s",
            normalised,
            exc.__class__.__name__,
        )
        raise NcbiServiceUnavailable("NCBI GenBank XML rejected by safe parser") from exc

    seq = root.find("GBSeq") if root.tag == "GBSet" else root
    if seq is None or seq.tag != "GBSeq":
        raise NcbiServiceUnavailable("NCBI GenBank XML missing GBSeq element")

    accession_version = (
        _xml_text(seq.find("GBSeq_accession-version")) or normalised
    )
    locus = _xml_text(seq.find("GBSeq_locus")) or accession_version
    length = _safe_int(_xml_text(seq.find("GBSeq_length")))
    moltype = _xml_text(seq.find("GBSeq_moltype")).lower() or None
    topology = _xml_text(seq.find("GBSeq_topology")).lower() or None
    strandedness = _xml_text(seq.find("GBSeq_strandedness")).lower() or None
    division = _xml_text(seq.find("GBSeq_division")) or None
    update_date = _xml_text(seq.find("GBSeq_update-date")) or None
    create_date = _xml_text(seq.find("GBSeq_create-date")) or None
    organism = _xml_text(seq.find("GBSeq_organism")) or None
    taxonomy_lineage = _xml_text(seq.find("GBSeq_taxonomy")) or ""
    definition = _xml_text(seq.find("GBSeq_definition")) or ""
    comment = _xml_text(seq.find("GBSeq_comment")) or ""
    source = _xml_text(seq.find("GBSeq_source")) or None

    # Clip the potentially large free-text fields, tracking which ones were
    # actually truncated so the UI can flag "view full record on NCBI".
    definition_clipped, definition_truncated = _truncate_flagged(definition)
    comment_clipped, comment_truncated = _truncate_flagged(comment, limit=4000)
    lineage_clipped, lineage_truncated = _truncate_flagged(
        taxonomy_lineage, limit=4000
    )
    truncated_fields: list[str] = []
    if definition_truncated:
        truncated_fields.append("definition")
    if comment_truncated:
        truncated_fields.append("comment")
    if lineage_truncated:
        truncated_fields.append("taxonomy_lineage")

    references = _parse_references(seq.find("GBSeq_references"))
    features = _parse_features(seq.find("GBSeq_feature-table"))
    xrefs = _parse_xrefs(seq.find("GBSeq_xrefs"))
    keywords = _parse_keywords(seq.find("GBSeq_keywords"))
    primary_accession = _xml_text(seq.find("GBSeq_primary-accession")) or None
    gi, other_seqids = _parse_other_seqids(seq.find("GBSeq_other-seqids"))
    secondary_accessions = _parse_secondary_accessions(
        seq.find("GBSeq_secondary-accessions")
    )

    return {
        "accession": accession_version.split(".")[0],
        "accession_version": accession_version,
        "primary_accession": primary_accession,
        "gi": gi,
        "other_seqids": other_seqids,
        "secondary_accessions": secondary_accessions,
        "locus": locus,
        "definition": definition_clipped,
        "length": length,
        "moltype": moltype,
        "topology": topology,
        "strandedness": strandedness,
        "division": division,
        "create_date": create_date,
        "update_date": update_date,
        "organism": organism,
        "taxonomy_lineage": lineage_clipped,
        "keywords": keywords,
        "source": source,
        "comment": comment_clipped,
        "truncated_fields": truncated_fields,
        "features": features,
        "references": references,
        "xrefs": xrefs,
        "data_source": "ncbi_eutils",
    }


def _parse_other_seqids(node: Any) -> tuple[str | None, list[str]]:
    """Parse ``GBSeq_other-seqids`` into a GI number + the raw seqid list.

    NCBI renders these in the record header (e.g. ``gi|568815587``,
    ``ref|NM_000546.6|``). The Sequence Detail page surfaces the GI number as a
    labelled field and keeps the remaining identifiers for completeness.
    """
    if node is None:
        return (None, [])
    gi: str | None = None
    out: list[str] = []
    for seqid in node.findall("GBSeqid"):
        value = _xml_text(seqid)
        if not value:
            continue
        if gi is None and value.lower().startswith("gi|"):
            tail = value.split("|", 1)[1].strip()
            if tail:
                gi = _truncate(tail, limit=40)
        out.append(_truncate(value, limit=120))
        if len(out) >= 16:
            break
    return (gi, out)


def _parse_secondary_accessions(node: Any) -> list[str]:
    """Parse ``GBSeq_secondary-accessions`` (the ``ACCESSION`` continuation)."""
    if node is None:
        return []
    out: list[str] = []
    for accn in node.findall("GBSecondary-accn"):
        value = _xml_text(accn)
        if not value:
            continue
        out.append(_truncate(value, limit=40))
        if len(out) >= 32:
            break
    return out


def _parse_keywords(node: Any) -> list[str]:
    """Parse the record-level ``GBSeq_keywords`` block into a string list.

    NCBI renders these as the ``KEYWORDS`` line of the GenBank flat file (and
    shows ``KEYWORDS  .`` when the list is empty). The Sequence Detail page
    reproduces that line, so it needs the parsed values rather than the raw
    XML node.
    """
    if node is None:
        return []
    out: list[str] = []
    for keyword in node.findall("GBKeyword"):
        value = _xml_text(keyword)
        if not value:
            continue
        out.append(_truncate(value, limit=120))
        if len(out) >= 32:
            break
    return out


def _parse_xrefs(node: Any) -> list[dict[str, str]]:
    """Parse the record-level DBLINK block (``GBSeq_xrefs``).

    NCBI surfaces BioProject / BioSample / Assembly / SRA accessions here
    rather than as ``source`` feature qualifiers, so the Sequence Detail page
    needs this list to reproduce the DBLINK section of the nuccore record.
    """
    if node is None:
        return []
    out: list[dict[str, str]] = []
    for xref in node.findall("GBXref"):
        dbname = _xml_text(xref.find("GBXref_dbname"))
        xid = _xml_text(xref.find("GBXref_id"))
        if not dbname or not xid:
            continue
        out.append(
            {
                "dbname": _truncate(dbname, limit=120),
                "id": _truncate(xid, limit=200),
            }
        )
        if len(out) >= 32:
            break
    return out


def _parse_features(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    out: list[dict[str, Any]] = []
    for feature_node in node.findall("GBFeature"):
        if len(out) >= MAX_FEATURES_PER_RECORD:
            break
        key = _xml_text(feature_node.find("GBFeature_key"))
        location = _xml_text(feature_node.find("GBFeature_location"))
        if not key:
            continue
        intervals = _parse_intervals(feature_node.find("GBFeature_intervals"))
        qualifiers = _parse_qualifiers(feature_node.find("GBFeature_quals"))
        from_pos: int | None = None
        to_pos: int | None = None
        strand_marker: str | None = None
        if intervals:
            # Use the union of intervals so the UI can show one (from, to)
            # while the per-interval list is also preserved.
            from_pos = min(i["from"] for i in intervals)
            to_pos = max(i["to"] for i in intervals)
            strands = {i.get("strand") for i in intervals if i.get("strand")}
            if strands == {"minus"}:
                strand_marker = "minus"
            elif strands == {"plus"}:
                strand_marker = "plus"
            else:
                strand_marker = "mixed" if strands else None
        out.append(
            {
                "key": key,
                "location": _truncate(location, limit=600),
                "from": from_pos,
                "to": to_pos,
                "strand": strand_marker,
                "intervals": intervals,
                "qualifiers": qualifiers,
            }
        )
    return out


def _parse_intervals(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    out: list[dict[str, Any]] = []
    for interval in node.findall("GBInterval"):
        start = _safe_int(_xml_text(interval.find("GBInterval_from")))
        end = _safe_int(_xml_text(interval.find("GBInterval_to")))
        if start is None or end is None:
            point = _safe_int(_xml_text(interval.find("GBInterval_point")))
            if point is None:
                continue
            start = end = point
        strand = "minus" if (end is not None and start is not None and end < start) else "plus"
        # Normalise to (min, max) but keep direction in `strand`.
        a, b = sorted((start, end))
        out.append({"from": a, "to": b, "strand": strand})
    return out


def _parse_qualifiers(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    out: list[dict[str, Any]] = []
    for qual in node.findall("GBQualifier"):
        name = _xml_text(qual.find("GBQualifier_name"))
        value = _xml_text(qual.find("GBQualifier_value"))
        if not name:
            continue
        clipped, truncated = _truncate_flagged(value, limit=400)
        out.append({"name": name, "value": clipped, "truncated": truncated})
    return out


def _parse_references(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    out: list[dict[str, Any]] = []
    for ref in node.findall("GBReference"):
        title = _xml_text(ref.find("GBReference_title"))
        journal = _xml_text(ref.find("GBReference_journal"))
        authors_node = ref.find("GBReference_authors")
        authors: list[str] = []
        if authors_node is not None:
            for author in authors_node.findall("GBAuthor"):
                txt = _xml_text(author)
                if txt:
                    authors.append(_truncate(txt, limit=200))
        pubmed = _xml_text(ref.find("GBReference_pubmed")) or None
        reference = _xml_text(ref.find("GBReference_reference")) or None
        consortium = _xml_text(ref.find("GBReference_consortium")) or None
        remark = _truncate(_xml_text(ref.find("GBReference_remark")), limit=1200) or None
        doi = _parse_reference_doi(ref.find("GBReference_xref"))
        if not (title or journal or authors):
            continue
        out.append(
            {
                "reference": _truncate(reference, limit=120) if reference else None,
                "title": _truncate(title, limit=400),
                "journal": _truncate(journal, limit=400),
                "authors": authors,
                "consortium": _truncate(consortium, limit=300) if consortium else None,
                "pubmed": pubmed,
                "doi": doi,
                "remark": remark,
            }
        )
    return out


def _parse_reference_doi(node: Any) -> str | None:
    """Extract the DOI from a ``GBReference_xref`` block, if present."""
    if node is None:
        return None
    for xref in node.findall("GBXref"):
        dbname = _xml_text(xref.find("GBXref_dbname")).lower()
        if dbname == "doi":
            doi = _xml_text(xref.find("GBXref_id"))
            if doi:
                return _truncate(doi, limit=200)
    return None


# ---------------------------------------------------------------------------
# efetch FASTA
# ---------------------------------------------------------------------------
def fetch_nuccore_fasta(
    accession: str,
    *,
    seq_start: int | None = None,
    seq_stop: int | None = None,
) -> str:
    """Return FASTA text for an accession, optionally subranged (1-based).

    ``seq_start``/``seq_stop`` follow NCBI semantics: when ``seq_start >
    seq_stop`` NCBI returns the reverse-complement of the range. Pass both or
    neither.
    """
    normalised = normalise_accession(accession)
    start_norm, stop_norm = _normalise_subrange(seq_start, seq_stop)
    cache_key = (normalised, start_norm, stop_norm)
    cached = _cache_get(_FASTA_CACHE, cache_key)
    if isinstance(cached, str):
        return cached
    params: dict[str, str] = {
        "db": "nuccore",
        "id": normalised,
        "rettype": "fasta",
        "retmode": "text",
    }
    if start_norm is not None and stop_norm is not None:
        params["seq_start"] = str(start_norm)
        params["seq_stop"] = str(stop_norm)
    body = request_bytes(
        "efetch.fcgi",
        params,
        max_bytes=MAX_FASTA_BYTES,
        accept="text/plain",
    )
    text = body.decode("utf-8", errors="replace").strip()
    if not text.startswith(">"):
        raise NcbiServiceUnavailable("NCBI FASTA response is not a FASTA record")
    _cache_put(_FASTA_CACHE, cache_key, text)
    return text


def _normalise_subrange(
    seq_start: int | None, seq_stop: int | None
) -> tuple[int | None, int | None]:
    if seq_start is None and seq_stop is None:
        return (None, None)
    if seq_start is None or seq_stop is None:
        raise ValueError("seq_start and seq_stop must both be provided or both omitted")
    for label, value in (("seq_start", seq_start), ("seq_stop", seq_stop)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be a positive integer")
        if value <= 0 or value > 10**10:
            raise ValueError(f"{label} must be between 1 and 10^10")
    return (seq_start, seq_stop)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _xml_text(node: Any) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


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


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truncate(value: str | None, *, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    # ``limit`` is retained for call-site compatibility but no longer clips:
    # the Sequence Detail page shows full field values. Whitespace is still
    # normalised for clean single-line display. Oversized records are bounded
    # upstream by the MAX_*_BYTES fetch caps, not here.
    if value is None:
        return ""
    return " ".join(value.split())


def _truncate_flagged(
    value: str | None, *, limit: int = MAX_DESCRIPTION_CHARS
) -> tuple[str, bool]:
    """Normalise whitespace and always report no truncation.

    Field truncation was removed: the Sequence Detail page renders full
    values. The ``bool`` flag (and the ``truncated_fields`` / qualifier
    ``truncated`` payload entries derived from it) are kept for response
    contract backward compatibility, so they always report ``False`` and the
    frontend's "view full on NCBI" affordances simply never trigger.
    Oversized records are bounded upstream by the MAX_*_BYTES fetch caps.
    """
    if value is None:
        return ("", False)
    return (" ".join(value.split()), False)
