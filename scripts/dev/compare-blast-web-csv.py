#!/usr/bin/env python3
"""Compare an NCBI Web BLAST CSV export with BLAST tabular output.

The Web CSV export is treated as the reference. The candidate is BLAST outfmt 6
with the standard 12 columns: qseqid, sseqid, pident, length, mismatch, gapopen,
qstart, qend, sstart, send, evalue, bitscore.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

WEB_COLUMNS = [
    "accession",
    "identity_pct",
    "coverage_pct",
    "evalue",
    "bits",
    "align_length",
    "gaps",
    "query_from",
    "query_to",
    "hit_from",
    "hit_to",
]

VALUE_COLUMNS = [
    "identity_pct",
    "evalue",
    "bits",
    "align_length",
    "gaps",
    "query_from",
    "query_to",
    "hit_from",
    "hit_to",
]

OUTFMT6_COLUMNS = [
    "query_id",
    "accession",
    "identity_pct",
    "align_length",
    "mismatches",
    "gaps",
    "query_from",
    "query_to",
    "hit_from",
    "hit_to",
    "evalue",
    "bits",
]


@dataclass(frozen=True)
class Row:
    rank: int
    values: dict[str, str]


def _decimal_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if number.is_zero():
        return "0"
    normalized = number.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f").rstrip("0").rstrip(".")


def _read_web_csv(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(set(WEB_COLUMNS) - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
        rows = []
        for index, record in enumerate(reader, start=1):
            values = {column: (record.get(column) or "").strip() for column in WEB_COLUMNS}
            rows.append(Row(rank=index, values=values))
        return rows


def _read_outfmt6(path: Path, query_id: str | None) -> list[Row]:
    rows: list[Row] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(OUTFMT6_COLUMNS):
                raise ValueError(f"{path} contains a row with fewer than 12 outfmt6 columns")
            values = dict(zip(OUTFMT6_COLUMNS, parts[: len(OUTFMT6_COLUMNS)], strict=True))
            if query_id and values["query_id"] != query_id:
                continue
            rows.append(Row(rank=len(rows) + 1, values=values))
    return rows


def _normalise_accession(accession: str) -> str:
    return accession.strip()


def _normalised_values(row: Row) -> dict[str, str | None]:
    return {column: _decimal_text(row.values.get(column)) for column in VALUE_COLUMNS}


def _first_mismatch(left: list[str], right: list[str]) -> dict[str, Any] | None:
    for rank, (left_value, right_value) in enumerate(zip(left, right, strict=False), start=1):
        if left_value != right_value:
            return {"rank": rank, "web": left_value, "candidate": right_value}
    if len(left) != len(right):
        return {
            "rank": min(len(left), len(right)) + 1,
            "web": left[min(len(left), len(right))] if len(left) > len(right) else None,
            "candidate": right[min(len(left), len(right))] if len(right) > len(left) else None,
        }
    return None


def compare(web_rows: list[Row], candidate_rows: list[Row]) -> dict[str, Any]:
    web_accessions = [_normalise_accession(row.values["accession"]) for row in web_rows]
    candidate_accessions = [_normalise_accession(row.values["accession"]) for row in candidate_rows]
    web_by_accession = {
        accession: row for accession, row in zip(web_accessions, web_rows, strict=True)
    }
    candidate_by_accession = {
        accession: row for accession, row in zip(candidate_accessions, candidate_rows, strict=True)
    }
    shared = sorted(set(web_accessions) & set(candidate_accessions))

    mismatches = []
    for accession in shared:
        web_values = _normalised_values(web_by_accession[accession])
        candidate_values = _normalised_values(candidate_by_accession[accession])
        differences = {
            column: {"web": web_values[column], "candidate": candidate_values[column]}
            for column in VALUE_COLUMNS
            if web_values[column] != candidate_values[column]
        }
        if differences:
            mismatches.append(
                {
                    "accession": accession,
                    "web_rank": web_by_accession[accession].rank,
                    "candidate_rank": candidate_by_accession[accession].rank,
                    "differences": differences,
                }
            )

    exact_order = web_accessions == candidate_accessions
    return {
        "equivalent": exact_order and not mismatches,
        "web_rows": len(web_rows),
        "candidate_rows": len(candidate_rows),
        "shared_accessions": len(shared),
        "web_only": len(set(web_accessions) - set(candidate_accessions)),
        "candidate_only": len(set(candidate_accessions) - set(web_accessions)),
        "same_top_accession": bool(
            web_accessions
            and candidate_accessions
            and web_accessions[0] == candidate_accessions[0]
        ),
        "top10_overlap": len(set(web_accessions[:10]) & set(candidate_accessions[:10])),
        "top100_overlap": len(set(web_accessions[:100]) & set(candidate_accessions[:100])),
        "exact_order": exact_order,
        "first_order_mismatch": _first_mismatch(web_accessions, candidate_accessions),
        "value_mismatch_count": len(mismatches),
        "first_10_value_mismatches": mismatches[:10],
        "web_top10": web_accessions[:10],
        "candidate_top10": candidate_accessions[:10],
    }


def build_report(web_csv: Path, candidate: Path, *, query_id: str | None) -> dict[str, Any]:
    web_rows = _read_web_csv(web_csv)
    candidate_rows = _read_outfmt6(candidate, query_id)
    report = compare(web_rows, candidate_rows)
    report.update({"web_csv": str(web_csv), "candidate": str(candidate), "query_id": query_id})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--web-csv", required=True, type=Path, help="NCBI Web BLAST CSV export"
    )
    parser.add_argument(
        "--candidate", required=True, type=Path, help="BLAST outfmt 6 candidate output"
    )
    parser.add_argument("--query-id", help="only compare candidate rows for this qseqid")
    parser.add_argument("--json", type=Path, help="optional JSON report output path")
    args = parser.parse_args(argv)

    report = build_report(args.web_csv, args.candidate, query_id=args.query_id)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json:
        args.json.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
