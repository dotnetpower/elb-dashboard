#!/usr/bin/env python3
"""Compare NCBI Web BLAST XML with BLAST outfmt 6 rows.

This is a fast equivalence helper for current Web BLAST probes: fetch XML for a
RID, run a tiny direct BLAST probe against cached shard/full DB data, then
compare accession order and primary HSP value fields without waiting for a full
ElasticBLAST submit/finalizer cycle.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

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

OPTIONAL_VALUE_COLUMNS = ["score"]

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

OPTIONAL_OUTFMT6_COLUMNS = ["score"]


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


def _versioned_accession(hit: ET.Element) -> str:
    hit_id = (hit.findtext("Hit_id") or "").strip()
    for pattern in (r"\|([A-Z]{1,4}_?\d+(?:\.\d+)?)\|?$", r"\|([A-Z]{1,4}_?\d+\.\d+)\|"):
        match = re.search(pattern, hit_id)
        if match:
            return match.group(1)
    return (hit.findtext("Hit_accession") or "").strip()


def _read_web_xml(path: Path) -> list[Row]:
    root = ET.parse(path).getroot()  # noqa: S314 - BLAST XML evidence files.
    rows: list[Row] = []
    query_id = root.findtext(".//Iteration_query-ID") or root.findtext(
        ".//Iteration_query-def"
    ) or "Query_1"
    for hit in root.findall(".//Iteration_hits/Hit"):
        hsp = hit.find("Hit_hsps/Hsp")
        if hsp is None:
            continue
        identity = int(hsp.findtext("Hsp_identity") or "0")
        align_length = int(hsp.findtext("Hsp_align-len") or "0")
        gaps = int(hsp.findtext("Hsp_gaps") or "0")
        identity_pct = Decimal(identity) * Decimal(100) / Decimal(align_length)
        rows.append(
            Row(
                rank=len(rows) + 1,
                values={
                    "query_id": query_id,
                    "accession": _versioned_accession(hit),
                    "identity_pct": format(identity_pct.quantize(Decimal("0.001")), "f"),
                    "align_length": str(align_length),
                    "mismatches": str(max(0, align_length - identity - gaps)),
                    "gaps": str(gaps),
                    "query_from": hsp.findtext("Hsp_query-from") or "",
                    "query_to": hsp.findtext("Hsp_query-to") or "",
                    "hit_from": hsp.findtext("Hsp_hit-from") or "",
                    "hit_to": hsp.findtext("Hsp_hit-to") or "",
                    "evalue": hsp.findtext("Hsp_evalue") or "",
                    "bits": hsp.findtext("Hsp_bit-score") or "",
                    "score": hsp.findtext("Hsp_score") or "",
                },
            )
        )
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
            for column, value in zip(
                OPTIONAL_OUTFMT6_COLUMNS,
                parts[len(OUTFMT6_COLUMNS) : len(OUTFMT6_COLUMNS) + len(OPTIONAL_OUTFMT6_COLUMNS)],
                strict=False,
            ):
                values[column] = value
            if query_id and values["query_id"] != query_id:
                continue
            rows.append(Row(rank=len(rows) + 1, values=values))
    return rows


def _normalised_values(row: Row) -> dict[str, str | None]:
    return {
        column: _decimal_text(row.values.get(column))
        for column in [*VALUE_COLUMNS, *OPTIONAL_VALUE_COLUMNS]
    }


def _value_differences(
    web_values: dict[str, str | None], candidate_values: dict[str, str | None]
) -> dict[str, dict[str, str | None]]:
    differences = {
        column: {"web": web_values[column], "candidate": candidate_values[column]}
        for column in VALUE_COLUMNS
        if web_values[column] != candidate_values[column]
    }
    for column in OPTIONAL_VALUE_COLUMNS:
        if candidate_values.get(column) is not None and web_values[column] != candidate_values[column]:
            differences[column] = {"web": web_values[column], "candidate": candidate_values[column]}
    if (
        "bits" in differences
        and candidate_values.get("score") is not None
        and web_values.get("score") == candidate_values.get("score")
    ):
        differences.pop("bits")
    return differences


def _first_mismatch(left: list[str], right: list[str]) -> dict[str, Any] | None:
    for rank, (left_value, right_value) in enumerate(zip(left, right, strict=False), start=1):
        if left_value != right_value:
            return {"rank": rank, "web": left_value, "candidate": right_value}
    if len(left) != len(right):
        index = min(len(left), len(right))
        return {
            "rank": index + 1,
            "web": left[index] if len(left) > index else None,
            "candidate": right[index] if len(right) > index else None,
        }
    return None


def compare(web_rows: list[Row], candidate_rows: list[Row]) -> dict[str, Any]:
    web_accessions = [row.values["accession"] for row in web_rows]
    candidate_accessions = [row.values["accession"] for row in candidate_rows]
    web_by_accession = {
        accession: row for accession, row in zip(web_accessions, web_rows, strict=True)
    }
    candidate_by_accession = {
        accession: row
        for accession, row in zip(candidate_accessions, candidate_rows, strict=True)
    }
    shared = sorted(set(web_accessions) & set(candidate_accessions))

    mismatches = []
    for accession in shared:
        web_values = _normalised_values(web_by_accession[accession])
        candidate_values = _normalised_values(candidate_by_accession[accession])
        differences = _value_differences(web_values, candidate_values)
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


def build_report(web_xml: Path, candidate: Path, *, query_id: str | None) -> dict[str, Any]:
    web_rows = _read_web_xml(web_xml)
    candidate_rows = _read_outfmt6(candidate, query_id)
    report = compare(web_rows, candidate_rows)
    report.update({"web_xml": str(web_xml), "candidate": str(candidate), "query_id": query_id})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-xml", required=True, type=Path, help="NCBI Web BLAST XML")
    parser.add_argument("--candidate", required=True, type=Path, help="BLAST outfmt 6 output")
    parser.add_argument("--query-id", help="only compare candidate rows for this qseqid")
    parser.add_argument("--json", type=Path, help="optional JSON report output path")
    args = parser.parse_args(argv)

    report = build_report(args.web_xml, args.candidate, query_id=args.query_id)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json:
        args.json.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())