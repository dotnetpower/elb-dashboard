#!/usr/bin/env python3
"""Compare BLAST XML outputs using canonical result fields.

Responsibility: Compare BLAST XML outputs using canonical result fields
Edit boundaries: Keep this as an operator/dev utility; do not make production code depend on it.
Key entry points: `Difference`, `_read_xml`, `_text`, `canonicalize`, `compare`, `build_report`
Risky contracts: Assume local developer context only; avoid broad production-side effects.
Validation: `uv run python scripts/dev/compare-blast-xml.py --help`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROVENANCE_FIELDS = {
    "BlastOutput_db",
}

TOP_LEVEL_FIELDS = [
    "BlastOutput_program",
    "BlastOutput_version",
    "BlastOutput_reference",
]

ITERATION_FIELDS = [
    "Iteration_query-ID",
    "Iteration_query-def",
    "Iteration_query-len",
]

STATISTIC_FIELDS = [
    "Statistics_db-num",
    "Statistics_db-len",
    "Statistics_hsp-len",
    "Statistics_eff-space",
    "Statistics_kappa",
    "Statistics_lambda",
    "Statistics_entropy",
]

HIT_FIELDS = [
    "Hit_id",
    "Hit_def",
    "Hit_accession",
    "Hit_len",
]

HSP_FIELDS = [
    "Hsp_bit-score",
    "Hsp_score",
    "Hsp_evalue",
    "Hsp_query-from",
    "Hsp_query-to",
    "Hsp_hit-from",
    "Hsp_hit-to",
    "Hsp_query-frame",
    "Hsp_hit-frame",
    "Hsp_identity",
    "Hsp_positive",
    "Hsp_gaps",
    "Hsp_align-len",
    "Hsp_qseq",
    "Hsp_hseq",
    "Hsp_midline",
]

NUMERIC_FIELDS = {
    "Hsp_bit-score",
    "Hsp_evalue",
    "Statistics_kappa",
    "Statistics_lambda",
    "Statistics_entropy",
}


@dataclass(frozen=True)
class Difference:
    path: str
    left: Any
    right: Any
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "left": self.left, "right": self.right, "reason": self.reason}


def _read_xml(path: Path) -> ET.Element:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:  # type: ignore[arg-type]
        return ET.parse(handle).getroot()  # noqa: S314 - BLAST XML evidence files.


def _text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    child = element.find(name)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text if text else None


def _normalize_numeric(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    if number.is_zero():
        return "0"
    return format(number.normalize(), "E")


def _field_value(element: ET.Element | None, name: str) -> str | None:
    value = _text(element, name)
    if name in NUMERIC_FIELDS:
        return _normalize_numeric(value)
    return value


def _normalize_db_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.rstrip("/")
    leaf = stripped.rsplit("/", 1)[-1]
    return re.sub(r"_shard_\d+$", "", leaf)


def _canonical_hsp(hsp: ET.Element) -> dict[str, str | None]:
    return {field: _field_value(hsp, field) for field in HSP_FIELDS}


def _canonical_hit(hit: ET.Element) -> dict[str, Any]:
    return {
        "fields": {field: _field_value(hit, field) for field in HIT_FIELDS},
        "hsps": [_canonical_hsp(hsp) for hsp in hit.findall("./Hit_hsps/Hsp")],
    }


def _canonical_iteration(iteration: ET.Element) -> dict[str, Any]:
    statistics = iteration.find("./Iteration_stat/Statistics")
    return {
        "fields": {field: _field_value(iteration, field) for field in ITERATION_FIELDS},
        "statistics": {field: _field_value(statistics, field) for field in STATISTIC_FIELDS},
        "hits": [_canonical_hit(hit) for hit in iteration.findall("./Iteration_hits/Hit")],
    }


def canonicalize(root: ET.Element) -> dict[str, Any]:
    if root.tag != "BlastOutput":
        raise ValueError(f"expected BlastOutput root, got {root.tag}")
    return {
        "top_level": {field: _field_value(root, field) for field in TOP_LEVEL_FIELDS},
        "provenance": {
            "BlastOutput_db": _field_value(root, "BlastOutput_db"),
            "BlastOutput_db_normalized": _normalize_db_name(_field_value(root, "BlastOutput_db")),
        },
        "iterations": [
            _canonical_iteration(iteration)
            for iteration in root.findall("./BlastOutput_iterations/Iteration")
        ],
    }


def _compare_value(path: str, left: Any, right: Any, differences: list[Difference]) -> None:
    if left != right:
        differences.append(Difference(path, left, right, "value_mismatch"))


def _compare_mapping(
    path: str,
    left: dict[str, Any],
    right: dict[str, Any],
    differences: list[Difference],
) -> None:
    keys = sorted(set(left) | set(right))
    for key in keys:
        _compare_any(f"{path}.{key}", left.get(key), right.get(key), differences)


def _compare_sequence(
    path: str,
    left: list[Any],
    right: list[Any],
    differences: list[Difference],
) -> None:
    if len(left) != len(right):
        differences.append(Difference(path, len(left), len(right), "length_mismatch"))
    for index, (left_item, right_item) in enumerate(zip(left, right, strict=False), start=1):
        _compare_any(f"{path}[{index}]", left_item, right_item, differences)


def _compare_any(path: str, left: Any, right: Any, differences: list[Difference]) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        _compare_mapping(path, left, right, differences)
    elif isinstance(left, list) and isinstance(right, list):
        _compare_sequence(path, left, right, differences)
    else:
        _compare_value(path, left, right, differences)


def compare(left: dict[str, Any], right: dict[str, Any], *, strict_db: bool) -> list[Difference]:
    differences: list[Difference] = []
    _compare_mapping("top_level", left["top_level"], right["top_level"], differences)
    _compare_sequence("iterations", left["iterations"], right["iterations"], differences)
    if strict_db:
        _compare_value(
            "provenance.BlastOutput_db_normalized",
            left["provenance"]["BlastOutput_db_normalized"],
            right["provenance"]["BlastOutput_db_normalized"],
            differences,
        )
    return differences


def _summary(canonical: dict[str, Any]) -> dict[str, Any]:
    iterations = canonical["iterations"]
    hit_count = sum(len(iteration["hits"]) for iteration in iterations)
    hsp_count = sum(len(hit["hsps"]) for iteration in iterations for hit in iteration["hits"])
    return {
        "program": canonical["top_level"].get("BlastOutput_program"),
        "version": canonical["top_level"].get("BlastOutput_version"),
        "db": canonical["provenance"].get("BlastOutput_db"),
        "db_normalized": canonical["provenance"].get("BlastOutput_db_normalized"),
        "queries": len(iterations),
        "hits": hit_count,
        "hsps": hsp_count,
    }


def build_report(left_path: Path, right_path: Path, *, strict_db: bool) -> dict[str, Any]:
    left = canonicalize(_read_xml(left_path))
    right = canonicalize(_read_xml(right_path))
    differences = compare(left, right, strict_db=strict_db)
    return {
        "equivalent": not differences,
        "strict_db": strict_db,
        "ignored_provenance_fields": [] if strict_db else sorted(PROVENANCE_FIELDS),
        "left_file": str(left_path),
        "right_file": str(right_path),
        "left_summary": _summary(left),
        "right_summary": _summary(right),
        "difference_count": len(differences),
        "differences": [difference.as_dict() for difference in differences],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", required=True, type=Path, help="reference BLAST XML file")
    parser.add_argument("--right", required=True, type=Path, help="candidate BLAST XML file")
    parser.add_argument("--json", type=Path, help="optional JSON report output path")
    parser.add_argument(
        "--strict-db",
        action="store_true",
        help="compare normalized BlastOutput_db values instead of treating them as provenance",
    )
    args = parser.parse_args(argv)

    report = build_report(args.left, args.right, strict_db=args.strict_db)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json:
        args.json.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
