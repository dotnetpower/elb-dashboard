"""Parser + aggregator for BLAST result output.

Ported from the retired Azure Functions tree so that
`/api/blast/jobs/{id}/results/aggregate`, `.../alignments`, and `.../export`
can return real data to the `BlastAnalytics` page instead of degraded stubs.

The parsers are deliberately **stateless and side-effect-free** so they can run
inside the api sidecar's HTTP handler without enqueuing a Celery task: the
blobs we parse are kilobytes-to-low-megabytes, capped by `max_bytes` in
`storage_data.read_result_blob_text`, so the cost is bounded.

Format support:
    * `-outfmt 5` (BLAST XML) — converted to the same canonical hit row shape
        used by tabular output so the UI can render Web BLAST-like tables and
        alignment previews.
  * `-outfmt 6` (tabular, no header) — assumed to use the BLAST default
    12-column layout (`qseqid sseqid pident length mismatch gapopen qstart
    qend sstart send evalue bitscore`).
  * `-outfmt 7` (tabular with `# Fields:` comment lines) — parsed by mapping
    the field labels back to canonical column names so custom `-outfmt 7
    'qseqid sseqid pident qlen slen ...'` invocations still work.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from defusedxml import ElementTree as ET

LOGGER = logging.getLogger(__name__)

# Default BLAST tabular (-outfmt 6) column order. Used when there is no
# `# Fields:` comment line in the file.
_DEFAULT_COLUMNS: tuple[str, ...] = (
    "qseqid",
    "sseqid",
    "pident",
    "length",
    "mismatch",
    "gapopen",
    "qstart",
    "qend",
    "sstart",
    "send",
    "evalue",
    "bitscore",
)

# Map the human-readable labels BLAST writes after `# Fields:` to the
# machine-readable column names used everywhere else.
_FIELD_LABEL_TO_COLUMN: dict[str, str] = {
    "query acc.ver": "qseqid",
    "query acc.": "qseqid",
    "query id": "qseqid",
    "subject acc.ver": "sseqid",
    "subject acc.": "sseqid",
    "subject id": "sseqid",
    "% identity": "pident",
    "alignment length": "length",
    "mismatches": "mismatch",
    "gap opens": "gapopen",
    "q. start": "qstart",
    "q. end": "qend",
    "s. start": "sstart",
    "s. end": "send",
    "evalue": "evalue",
    "bit score": "bitscore",
    "% positives": "ppos",
    "query length": "qlen",
    "subject length": "slen",
    "query seq": "qseq",
    "subject seq": "sseq",
    "subject title": "stitle",
    "subject sci name": "sscinames",
    "subject sci names": "sscinames",
    "subject taxids": "staxids",
}

# Columns that should be coerced to float, int, or left as string.
_FLOAT_COLUMNS = frozenset({"pident", "evalue", "bitscore", "ppos"})
_INT_COLUMNS = frozenset(
    {
        "length",
        "mismatch",
        "gapopen",
        "gaps",
        "qstart",
        "qend",
        "sstart",
        "send",
        "qlen",
        "slen",
        "score",
    }
)


def parse_blast_result_content(content: str) -> list[dict[str, Any]]:
    """Parse BLAST XML (`outfmt 5`) or tabular (`outfmt 6` / `outfmt 7`) content."""
    stripped = content.lstrip("\ufeff \t\r\n")
    if stripped.startswith("<?xml") or stripped.startswith("<BlastOutput"):
        return parse_blast_xml(stripped)
    return parse_blast_tabular(content)


def parse_blast_xml(content: str) -> list[dict[str, Any]]:
    """Parse BLAST XML (`-outfmt 5`) into canonical hit dictionaries.

    Each HSP becomes one row, matching the tabular parser's shape closely
    enough for aggregate stats, CSV export, and the alignment preview. BLAST
    XML reports total gap characters as `Hsp_gaps`; the historical UI field is
    named `gapopen`, so both `gapopen` and `gaps` carry that same value.
    """
    root = ET.fromstring(content)
    if _local_name(root.tag) != "BlastOutput":
        return []

    hits: list[dict[str, Any]] = []
    iterations_parent = _child(root, "BlastOutput_iterations")
    for iteration in _children(iterations_parent, "Iteration"):
        query_id = (
            _text(iteration, "Iteration_query-ID")
            or _text(iteration, "Iteration_query-def")
            or "Query_1"
        )
        query_len = _int_or_none(_text(iteration, "Iteration_query-len"))
        hits_parent = _child(iteration, "Iteration_hits")
        for hit in _children(hits_parent, "Hit"):
            subject_id = (
                _versioned_accession(hit) or _text(hit, "Hit_accession") or _text(hit, "Hit_id")
            )
            subject_title = _text(hit, "Hit_def")
            subject_len = _int_or_none(_text(hit, "Hit_len"))
            if not subject_id:
                continue
            hsps_parent = _child(hit, "Hit_hsps")
            for hsp in _children(hsps_parent, "Hsp"):
                align_len = _int_or_none(_text(hsp, "Hsp_align-len"))
                identity = _int_or_none(_text(hsp, "Hsp_identity"))
                gaps = _int_or_none(_text(hsp, "Hsp_gaps")) or 0
                positive = _int_or_none(_text(hsp, "Hsp_positive"))
                pident = (
                    round((identity * 100.0) / align_len, 3)
                    if identity is not None and align_len and align_len > 0
                    else None
                )
                ppos = (
                    round((positive * 100.0) / align_len, 3)
                    if positive is not None and align_len and align_len > 0
                    else None
                )
                row: dict[str, Any] = {
                    "qseqid": query_id,
                    "sseqid": subject_id,
                    "pident": pident,
                    "length": align_len,
                    "mismatch": (
                        max(0, align_len - identity - gaps)
                        if align_len is not None and identity is not None
                        else None
                    ),
                    "gapopen": gaps,
                    "gaps": gaps,
                    "qstart": _int_or_none(_text(hsp, "Hsp_query-from")),
                    "qend": _int_or_none(_text(hsp, "Hsp_query-to")),
                    "sstart": _int_or_none(_text(hsp, "Hsp_hit-from")),
                    "send": _int_or_none(_text(hsp, "Hsp_hit-to")),
                    "evalue": _float_or_none(_text(hsp, "Hsp_evalue")),
                    "bitscore": _float_or_none(_text(hsp, "Hsp_bit-score")),
                    "score": _int_or_none(_text(hsp, "Hsp_score")),
                    "qlen": query_len,
                    "slen": subject_len,
                    "stitle": subject_title or None,
                    "qseq": _text(hsp, "Hsp_qseq") or None,
                    "sseq": _text(hsp, "Hsp_hseq") or None,
                    "midline": _text(hsp, "Hsp_midline") or None,
                }
                if ppos is not None:
                    row["ppos"] = ppos
                hits.append({key: value for key, value in row.items() if value is not None})
    return hits


def parse_blast_tabular(content: str) -> list[dict[str, Any]]:
    """Parse BLAST tabular output (`-outfmt 6` or `-outfmt 7`).

    Returns a list of hit dicts. Numeric columns are coerced to int/float;
    string columns (qseqid, sseqid, stitle, sscinames, staxids, …) are
    kept verbatim so the UI can render them.

    Malformed lines with too few columns are skipped silently. Unparseable
    numeric values are preserved as text so the caller can still render the
    row and aggregation can ignore only the invalid metric.
    """
    hits: list[dict[str, Any]] = []
    columns: tuple[str, ...] = _DEFAULT_COLUMNS

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# Fields:"):
            field_str = line[len("# Fields:") :].strip()
            raw_fields = [f.strip() for f in field_str.split(",")]
            columns = tuple(
                _FIELD_LABEL_TO_COLUMN.get(label, label.replace(" ", "_").replace(".", ""))
                for label in raw_fields
            )
            continue
        if line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < len(columns):
            # Tolerate trailing-truncation artefacts in partial files but skip
            # the line — it's safer than emitting half-populated dicts that
            # break downstream numeric aggregation.
            continue

        hit: dict[str, Any] = {}
        for index, column in enumerate(columns):
            value = parts[index] if index < len(parts) else ""
            if column in _FLOAT_COLUMNS:
                try:
                    hit[column] = float(value)
                except ValueError:
                    hit[column] = value
            elif column in _INT_COLUMNS:
                try:
                    hit[column] = int(value)
                except ValueError:
                    hit[column] = value
            else:
                hit[column] = value
        hits.append(hit)

    return hits


def aggregate_blast_hits(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics over a list of parsed BLAST hits.

    Output schema matches `BlastAggregateStats` in `web/src/api/blast.ts`:

      {
        total_hits, unique_queries, unique_subjects,
        evalue_distribution: { "<bin>": <count>, ... },
        identity_distribution: { "<bin>": <count>, ... },
        top_subjects: [{ id, count }, ...],
        top_hit_per_query: [{ qseqid, sseqid, pident, evalue, bitscore }, ...],
        avg_identity, avg_bitscore, avg_length,
        max_bitscore, min_evalue,
      }
    """
    total = len(hits)
    unique_queries: set[str] = set()
    unique_subjects: set[str] = set()
    evalues: list[float] = []
    identities: list[float] = []
    bitscores: list[float] = []
    lengths: list[int] = []
    subject_counts: dict[str, int] = {}
    # Best hit (lowest evalue, then highest bitscore) per query for the
    # diagnostic "what is this?" view.
    best_per_query: dict[str, dict[str, Any]] = {}

    for hit in hits:
        qid = str(hit.get("qseqid", "") or "")
        sid = str(hit.get("sseqid", "") or "")
        if qid:
            unique_queries.add(qid)
        if sid:
            unique_subjects.add(sid)
            subject_counts[sid] = subject_counts.get(sid, 0) + 1

        evalue = hit.get("evalue")
        if isinstance(evalue, (int, float)) and evalue >= 0:
            evalues.append(float(evalue))
        pident = hit.get("pident")
        if isinstance(pident, (int, float)):
            identities.append(float(pident))
        bitscore = hit.get("bitscore")
        if isinstance(bitscore, (int, float)):
            bitscores.append(float(bitscore))
        length = hit.get("length")
        if isinstance(length, int):
            lengths.append(length)

        if qid:
            previous = best_per_query.get(qid)
            if previous is None or _hit_is_better(hit, previous):
                best_per_query[qid] = hit

    # E-value distribution (log10 bins, identical to legacy implementation).
    evalue_bins: dict[str, int] = {
        "0": 0,
        "1e-200..1e-100": 0,
        "1e-100..1e-50": 0,
        "1e-50..1e-10": 0,
        "1e-10..1e-5": 0,
        "1e-5..0.01": 0,
        "0.01..1": 0,
        "1..10": 0,
        ">10": 0,
    }
    for evalue in evalues:
        if evalue == 0:
            evalue_bins["0"] += 1
        elif evalue < 1e-100:
            evalue_bins["1e-200..1e-100"] += 1
        elif evalue < 1e-50:
            evalue_bins["1e-100..1e-50"] += 1
        elif evalue < 1e-10:
            evalue_bins["1e-50..1e-10"] += 1
        elif evalue < 1e-5:
            evalue_bins["1e-10..1e-5"] += 1
        elif evalue < 0.01:
            evalue_bins["1e-5..0.01"] += 1
        elif evalue < 1:
            evalue_bins["0.01..1"] += 1
        elif evalue <= 10:
            evalue_bins["1..10"] += 1
        else:
            evalue_bins[">10"] += 1

    # Identity % distribution (10% bins).
    identity_bins: dict[str, int] = {}
    for percent in range(0, 100, 10):
        label = f"{percent}-{percent + 10}%"
        identity_bins[label] = sum(1 for value in identities if percent <= value < percent + 10)
    identity_bins["100%"] = sum(1 for value in identities if value == 100)

    top_subjects = sorted(subject_counts.items(), key=lambda item: item[1], reverse=True)[:20]

    # Project the top-hit-per-query map back to a stable list, sorted by
    # query id for deterministic UI rendering.
    top_hit_per_query = [
        {
            "qseqid": qid,
            "sseqid": str(hit.get("sseqid", "") or ""),
            "pident": _as_float_or_none(hit.get("pident")),
            "evalue": _as_float_or_none(hit.get("evalue")),
            "bitscore": _as_float_or_none(hit.get("bitscore")),
            "length": hit.get("length") if isinstance(hit.get("length"), int) else None,
            "stitle": str(hit.get("stitle", "") or "") or None,
        }
        for qid, hit in sorted(best_per_query.items())
    ]

    return {
        "total_hits": total,
        "unique_queries": len(unique_queries),
        "unique_subjects": len(unique_subjects),
        "evalue_distribution": evalue_bins,
        "identity_distribution": identity_bins,
        "top_subjects": [{"id": sid, "count": count} for sid, count in top_subjects],
        "top_hit_per_query": top_hit_per_query,
        "avg_identity": round(sum(identities) / len(identities), 2) if identities else None,
        "avg_bitscore": round(sum(bitscores) / len(bitscores), 2) if bitscores else None,
        "avg_length": round(sum(lengths) / len(lengths), 1) if lengths else None,
        "max_bitscore": max(bitscores) if bitscores else None,
        "min_evalue": min(evalues) if evalues else None,
    }


def _hit_is_better(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """Lower evalue wins; ties broken by higher bitscore."""
    cand_e = candidate.get("evalue")
    curr_e = current.get("evalue")
    cand_b = candidate.get("bitscore")
    curr_b = current.get("bitscore")
    cand_e_val = float(cand_e) if isinstance(cand_e, (int, float)) else float("inf")
    curr_e_val = float(curr_e) if isinstance(curr_e, (int, float)) else float("inf")
    if cand_e_val < curr_e_val:
        return True
    if cand_e_val > curr_e_val:
        return False
    cand_b_val = float(cand_b) if isinstance(cand_b, (int, float)) else float("-inf")
    curr_b_val = float(curr_b) if isinstance(curr_b, (int, float)) else float("-inf")
    return cand_b_val > curr_b_val


def _as_float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


# Default column set used by the CSV / TSV exporter so the output looks like
# a `-outfmt 6` file the user could have downloaded directly from a Web BLAST
# job. Extra columns the parser captured (qlen, slen, stitle, …) are added on
# top of this when present.
EXPORT_DEFAULT_COLUMNS: tuple[str, ...] = _DEFAULT_COLUMNS
EXPORT_EXTRA_COLUMNS: tuple[str, ...] = (
    "qlen",
    "slen",
    "score",
    "gaps",
    "ppos",
    "qcovs",
    "scovs",
    "review_status",
    "review_reason",
    "source_blob",
    "stitle",
    "sscinames",
    "staxids",
    "qseq",
    "sseq",
    "midline",
)


def _text(element: ET.Element, name: str) -> str:
    child = _child(element, name)
    value = child.text if child is not None else None
    return value.strip() if value else ""


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in element if _local_name(child.tag) == name]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _versioned_accession(hit: ET.Element) -> str:
    hit_id = _text(hit, "Hit_id")
    for pattern in (r"\|([A-Z]{1,4}_?\d+(?:\.\d+)?)\|?$", r"\|([A-Z]{1,4}_?\d+\.\d+)\|"):
        match = re.search(pattern, hit_id)
        if match:
            return match.group(1)
    accession = _text(hit, "Hit_accession")
    return accession
