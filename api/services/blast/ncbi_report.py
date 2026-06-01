"""NCBI Web BLAST-style description-table and report rendering.

Responsibility: Aggregate per-HSP parsed BLAST hits into per-subject rows that
mirror the NCBI Web BLAST "Descriptions" table, and render them as a delimited
hit table or a plain-text report with a provenance header.
Edit boundaries: Pure rendering/aggregation only. No Azure SDK, HTTP, or Storage
work here; callers pass already-parsed hit dicts and job metadata.
Key entry points: `aggregate_ncbi_rows`, `format_ncbi_hit_table`,
`format_ncbi_report_text`, `NCBI_HIT_TABLE_COLUMNS`.
Risky contracts: Output is researcher-facing text; never include Storage URLs,
SAS tokens, or subscription identifiers. The synthetic RID is prefixed `ELB-` so
it is never mistaken for an NCBI-issued request id.
Validation: `uv run pytest -q api/tests/test_blast_ncbi_report.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Column order mirrors the NCBI Web BLAST "Descriptions" table.
NCBI_HIT_TABLE_COLUMNS = (
    "Description",
    "Scientific Name",
    "Common Name",
    "Taxid",
    "Max Score",
    "Total Score",
    "Query Cover",
    "E value",
    "Per. Ident",
    "Acc. Len",
    "Accession",
)


@dataclass
class _SubjectAccumulator:
    accession: str
    description: str = ""
    scientific_name: str = ""
    common_name: str = ""
    taxid: str = ""
    max_bitscore: float = float("-inf")
    total_bitscore: float = 0.0
    min_evalue: float = float("inf")
    best_pident: float = 0.0
    acc_len: int = 0
    query_len: int = 0
    _ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class NcbiRow:
    """One aggregated NCBI description-table row (per query+subject)."""

    query: str
    description: str
    scientific_name: str
    common_name: str
    taxid: str
    max_score: int
    total_score: int
    query_cover: int
    evalue: float
    per_ident: float
    acc_len: int
    accession: str

    def as_cells(self) -> list[str]:
        return [
            self.description,
            self.scientific_name,
            self.common_name,
            self.taxid,
            str(self.max_score),
            str(self.total_score),
            f"{self.query_cover}%",
            _format_evalue(self.evalue),
            f"{self.per_ident:.2f}%",
            str(self.acc_len) if self.acc_len else "",
            self.accession,
        ]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first_token(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    for sep in (";", "|"):
        if sep in text:
            return text.split(sep)[0].strip()
    return text.strip()


def _union_length(ranges: Iterable[tuple[int, int]]) -> int:
    """Total length covered by the union of 1-based inclusive ranges."""
    norm = sorted((min(a, b), max(a, b)) for a, b in ranges if a or b)
    if not norm:
        return 0
    total = 0
    cur_start, cur_end = norm[0]
    for start, end in norm[1:]:
        if start <= cur_end + 1:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start + 1
            cur_start, cur_end = start, end
    total += cur_end - cur_start + 1
    return total


def aggregate_ncbi_rows(hits: Iterable[Mapping[str, Any]]) -> list[NcbiRow]:
    """Collapse per-HSP hit dicts into per-(query, subject) NCBI rows.

    Aggregation rules follow the NCBI Web BLAST description table:
    - Max Score    = max bit score across the subject's HSPs
    - Total Score  = sum of bit scores
    - Query Cover  = union of query ranges / query length, as an integer percent
    - E value      = minimum expect value
    - Per. Ident   = percent identity of the highest-scoring HSP
    - Acc. Len     = subject sequence length
    """
    acc: dict[tuple[str, str], _SubjectAccumulator] = {}
    for hit in hits:
        query = str(hit.get("qseqid") or hit.get("query") or "")
        subject = str(hit.get("sseqid") or hit.get("subject") or "")
        if not subject:
            continue
        key = (query, subject)
        entry = acc.get(key)
        if entry is None:
            entry = _SubjectAccumulator(accession=subject)
            acc[key] = entry

        bitscore = _to_float(hit.get("bitscore"))
        evalue = _to_float(hit.get("evalue"), default=float("inf"))
        pident = _to_float(hit.get("pident"))
        qstart = _to_int(hit.get("qstart"))
        qend = _to_int(hit.get("qend"))

        entry.total_bitscore += bitscore
        if bitscore > entry.max_bitscore:
            entry.max_bitscore = bitscore
            entry.best_pident = pident
        if evalue < entry.min_evalue:
            entry.min_evalue = evalue
        if qstart or qend:
            entry._ranges.append((qstart, qend))

        stitle = str(hit.get("stitle") or "").strip()
        if stitle and not entry.description:
            entry.description = stitle
        scin = _first_token(hit.get("sscinames"))
        if scin and not entry.scientific_name:
            entry.scientific_name = scin
        taxid = _first_token(hit.get("staxids"))
        if taxid and not entry.taxid:
            entry.taxid = taxid
        slen = _to_int(hit.get("slen"))
        if slen and not entry.acc_len:
            entry.acc_len = slen
        qlen = _to_int(hit.get("qlen"))
        if qlen and not entry.query_len:
            entry.query_len = qlen

    rows: list[NcbiRow] = []
    for (query, subject), entry in acc.items():
        covered = _union_length(entry._ranges)
        query_cover = (
            min(100, round(covered / entry.query_len * 100)) if entry.query_len else 0
        )
        max_score = 0 if entry.max_bitscore == float("-inf") else round(entry.max_bitscore)
        evalue = 0.0 if entry.min_evalue == float("inf") else entry.min_evalue
        rows.append(
            NcbiRow(
                query=query,
                description=entry.description or subject,
                scientific_name=entry.scientific_name,
                common_name=entry.common_name,
                taxid=entry.taxid,
                max_score=max_score,
                total_score=round(entry.total_bitscore),
                query_cover=query_cover,
                evalue=evalue,
                per_ident=entry.best_pident,
                acc_len=entry.acc_len,
                accession=subject,
            )
        )

    # Sort by query, then descending Max Score (NCBI default ordering).
    rows.sort(key=lambda r: (r.query, -r.max_score, r.evalue))
    return rows


def _format_evalue(value: float) -> str:
    if value == 0.0:
        return "0.0"
    if value >= 0.001:
        return f"{value:.3g}"
    return f"{value:.2e}"


def format_ncbi_hit_table(rows: Iterable[NcbiRow], *, delimiter: str = "\t") -> str:
    """Render the aggregated rows as a delimited NCBI-style description table."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerow(NCBI_HIT_TABLE_COLUMNS)
    for row in rows:
        writer.writerow(row.as_cells())
    return buf.getvalue()


def format_ncbi_report_text(
    rows: Iterable[NcbiRow],
    *,
    rid: str,
    program: str,
    database: str,
    job_title: str | None = None,
    blast_version: str | None = None,
    database_snapshot: str | None = None,
    compatibility_note: str | None = None,
) -> str:
    """Render a plain-text NCBI-like report with a provenance header."""
    rows = list(rows)
    lines: list[str] = []
    lines.append(f"RID: {rid}")
    if job_title:
        lines.append(f"Job Title: {job_title}")
    lines.append(f"Program: {(program or 'BLAST').upper()}")
    if blast_version:
        lines.append(f"BLAST version: {blast_version}")
    lines.append(f"Database: {database or 'unknown'}")
    if database_snapshot:
        lines.append(f"Database snapshot: {database_snapshot}")
    if compatibility_note:
        lines.append(f"Compatibility: {compatibility_note}")
    lines.append(
        "Note: Generated by elb-dashboard from ElasticBLAST output on a "
        "self-managed Azure AKS cluster. Not an NCBI-issued report."
    )
    lines.append("")

    queries = sorted({r.query for r in rows})
    if not rows:
        lines.append("No hits found.")
        return "\n".join(lines) + "\n"

    for idx, query in enumerate(queries, start=1):
        q_rows = [r for r in rows if r.query == query]
        lines.append(f"Query #{idx}: {query} ({len(q_rows)} subject sequence(s))")
        lines.append("Sequences producing significant alignments:")
        header_cells = list(NCBI_HIT_TABLE_COLUMNS)
        table_rows = [r.as_cells() for r in q_rows]
        lines.extend(_render_fixed_width(header_cells, table_rows))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_fixed_width(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header))]
    out.append("  ".join("-" * widths[i] for i in range(len(header))))
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return out
