#!/usr/bin/env python3
# ruff: noqa: E501
"""Evaluate candidate tie-order keys for Web BLAST equivalence evidence.

This is an offline diagnostic helper. It never changes BLAST output; it records
which deterministic keys can or cannot explain Web BLAST's ordering through a
large tied score class.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import zlib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Candidate:
    accession: str
    query_id: str
    identity_pct: float
    align_length: int
    mismatches: int
    gaps: int
    qstart: int
    qend: int
    sstart: int
    send: int
    evalue: float
    bits: float
    score: float | None
    pool_ordinal: int
    local_oid: int | None = None
    gi: int | None = None
    sequence_length: int | None = None
    taxid: str | None = None
    shard: int | None = None
    title: str = ""
    volume: int | None = None
    volume_oid: int | None = None


SortKey = Callable[[Candidate], tuple[Any, ...]]


def _read_candidates(path: Path) -> dict[str, Candidate]:
    candidates: dict[str, Candidate] = {}
    for ordinal, raw_line in enumerate(path.read_text().splitlines()):
        if not raw_line.strip() or raw_line.startswith("#"):
            continue
        cols = raw_line.split("\t")
        if len(cols) < 12:
            continue
        score = None
        if len(cols) > 12:
            try:
                score = float(cols[12])
            except ValueError:
                score = None
        candidates[cols[1]] = Candidate(
            accession=cols[1],
            query_id=cols[0],
            identity_pct=float(cols[2]),
            align_length=int(cols[3]),
            mismatches=int(cols[4]),
            gaps=int(cols[5]),
            qstart=int(cols[6]),
            qend=int(cols[7]),
            sstart=int(cols[8]),
            send=int(cols[9]),
            evalue=float(cols[10]),
            bits=float(cols[11]),
            score=score,
            pool_ordinal=ordinal,
        )
    return candidates


def _with_metadata(candidates: dict[str, Candidate], metadata_path: Path, volume_path: Path) -> list[Candidate]:
    metadata: dict[str, dict[str, Any]] = {}
    for raw_line in metadata_path.read_text().splitlines():
        cols = raw_line.split("\t")
        if len(cols) < 6:
            continue
        metadata[cols[0]] = {
            "local_oid": _int_or_none(cols[1]),
            "gi": _int_or_none(cols[2]),
            "sequence_length": _int_or_none(cols[3]),
            "taxid": cols[4],
            "shard": _int_or_none(cols[5]),
            "title": cols[6] if len(cols) > 6 else "",
        }

    volumes: dict[str, dict[str, int | None]] = {}
    for raw_line in volume_path.read_text().splitlines():
        cols = raw_line.split("\t")
        if len(cols) < 3:
            continue
        volumes[cols[0]] = {"volume": _int_or_none(cols[1]), "volume_oid": _int_or_none(cols[2])}

    enriched: list[Candidate] = []
    for accession, candidate in candidates.items():
        meta = metadata.get(accession, {})
        volume = volumes.get(accession, {})
        enriched.append(
            Candidate(
                **{
                    **candidate.__dict__,
                    "local_oid": meta.get("local_oid"),
                    "gi": meta.get("gi"),
                    "sequence_length": meta.get("sequence_length"),
                    "taxid": meta.get("taxid"),
                    "shard": meta.get("shard"),
                    "title": meta.get("title", ""),
                    "volume": volume.get("volume"),
                    "volume_oid": volume.get("volume_oid"),
                }
            )
        )
    return enriched


def _read_web_accessions(path: Path) -> list[str]:
    accessions: list[str] = []
    for raw_line in path.read_text().splitlines():
        if not raw_line.strip():
            continue
        cols = raw_line.split("\t")
        accessions.append(cols[1] if len(cols) > 1 and cols[0].isdigit() else cols[0])
    return accessions


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _year(title: str) -> int:
    matches = [int(item) for item in re.findall(r"\b(?:19|20)\d{2}\b", title)]
    return max(matches) if matches else -1


def _accession_number(accession: str) -> int:
    match = re.search(r"(\d+)", accession)
    return int(match.group(1)) if match else -1


def _accession_version(accession: str) -> int:
    if "." not in accession:
        return 0
    return _int_or_none(accession.rsplit(".", 1)[1]) or 0


def _prefix(accession: str, width: int) -> str:
    return accession[:width]


def _hash_int(text: str, algorithm: str) -> int:
    if algorithm == "crc32":
        return zlib.crc32(text.encode("utf-8"))
    digest = hashlib.new(algorithm, text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _none_high(value: int | float | None) -> int | float:
    if value is None:
        return 10**18
    return value


def _none_low(value: int | float | None) -> int | float:
    if value is None:
        return -(10**18)
    return value


def _desc_text(text: str) -> tuple[int, ...]:
    return tuple(-ord(char) for char in text)


def _primary_score(candidate: Candidate) -> tuple[Any, ...]:
    return (candidate.evalue, -candidate.bits, -(candidate.score or candidate.bits))


def _evaluate(
    name: str,
    candidates: list[Candidate],
    key: SortKey,
    web_accessions: list[str],
) -> dict[str, Any]:
    web_set = set(web_accessions)
    ordered = sorted(candidates, key=lambda candidate: (*_primary_score(candidate), *key(candidate)))
    selected = ordered[: len(web_accessions)]
    selected_accessions = [candidate.accession for candidate in selected]
    prefix_count = 0
    for web_accession, selected_accession in zip(web_accessions, selected_accessions, strict=False):
        if web_accession != selected_accession:
            break
        prefix_count += 1
    return {
        "name": name,
        "same_top": selected_accessions[:1] == web_accessions[:1],
        "prefix_match_count": prefix_count,
        "top10_overlap": len(set(selected_accessions[:10]) & set(web_accessions[:10])),
        "top100_overlap": len(set(selected_accessions[:100]) & set(web_accessions[:100])),
        "top500_overlap": len(set(selected_accessions) & web_set),
        "first10": selected_accessions[:10],
    }


def _base_strategies(web_accessions: list[str]) -> list[tuple[str, SortKey]]:
    web_prefix2_order = {
        prefix: rank for rank, prefix in enumerate(dict.fromkeys(_prefix(acc, 2) for acc in web_accessions))
    }
    web_prefix2_count = Counter(_prefix(acc, 2) for acc in web_accessions)
    web_prefix3_order = {
        prefix: rank for rank, prefix in enumerate(dict.fromkeys(_prefix(acc, 3) for acc in web_accessions))
    }
    return [
        ("pool_ordinal", lambda c: (c.pool_ordinal,)),
        ("pool_ordinal_desc", lambda c: (-c.pool_ordinal,)),
        ("accession_asc", lambda c: (c.accession,)),
        ("accession_desc", lambda c: (_desc_text(c.accession),)),
        ("accession_number_asc", lambda c: (_accession_number(c.accession), c.accession)),
        ("accession_number_desc", lambda c: (-_accession_number(c.accession), c.accession)),
        ("accession_version_desc", lambda c: (-_accession_version(c.accession), c.accession)),
        ("prefix2_web_observed", lambda c: (web_prefix2_order.get(_prefix(c.accession, 2), 999), c.accession)),
        ("prefix2_web_count_desc", lambda c: (-web_prefix2_count.get(_prefix(c.accession, 2), 0), c.accession)),
        ("prefix3_web_observed", lambda c: (web_prefix3_order.get(_prefix(c.accession, 3), 999), c.accession)),
        ("local_oid_asc", lambda c: (_none_high(c.local_oid), c.accession)),
        ("local_oid_desc", lambda c: (-_none_low(c.local_oid), c.accession)),
        ("gi_asc", lambda c: (_none_high(c.gi), c.accession)),
        ("gi_desc", lambda c: (-_none_low(c.gi), c.accession)),
        ("sequence_length_asc", lambda c: (_none_high(c.sequence_length), c.accession)),
        ("sequence_length_desc", lambda c: (-_none_low(c.sequence_length), c.accession)),
        ("shard_asc_local_oid", lambda c: (_none_high(c.shard), _none_high(c.local_oid), c.accession)),
        ("shard_desc_local_oid", lambda c: (-_none_low(c.shard), _none_high(c.local_oid), c.accession)),
        ("volume_oid_asc", lambda c: (_none_high(c.volume), _none_high(c.volume_oid), c.accession)),
        ("volume_oid_desc", lambda c: (-_none_low(c.volume), -_none_low(c.volume_oid), c.accession)),
        ("volume_asc_oid_desc", lambda c: (_none_high(c.volume), -_none_low(c.volume_oid), c.accession)),
        ("oid_asc", lambda c: (_none_high(c.volume_oid), c.accession)),
        ("oid_desc", lambda c: (-_none_low(c.volume_oid), c.accession)),
        ("sstart_asc", lambda c: (c.sstart, c.send, c.accession)),
        ("sstart_desc", lambda c: (-c.sstart, -c.send, c.accession)),
        ("send_asc", lambda c: (c.send, c.sstart, c.accession)),
        ("send_desc", lambda c: (-c.send, -c.sstart, c.accession)),
        ("year_desc", lambda c: (-_year(c.title), c.accession)),
        ("year_desc_gi_desc", lambda c: (-_year(c.title), -_none_low(c.gi), c.accession)),
        ("title_asc", lambda c: (c.title, c.accession)),
        ("title_desc", lambda c: (_desc_text(c.title), c.accession)),
        ("title_len_asc", lambda c: (len(c.title), c.accession)),
        ("title_len_desc", lambda c: (-len(c.title), c.accession)),
        ("crc32_accession_asc", lambda c: (_hash_int(c.accession, "crc32"), c.accession)),
        ("crc32_accession_desc", lambda c: (-_hash_int(c.accession, "crc32"), c.accession)),
        ("sha1_accession_asc", lambda c: (_hash_int(c.accession, "sha1"), c.accession)),
        ("sha1_accession_desc", lambda c: (-_hash_int(c.accession, "sha1"), c.accession)),
        ("md5_title_asc", lambda c: (_hash_int(c.title, "md5"), c.accession)),
        ("md5_title_desc", lambda c: (-_hash_int(c.title, "md5"), c.accession)),
    ]


def _pair_strategies() -> list[tuple[str, SortKey]]:
    features: dict[str, Callable[[Candidate], Any]] = {
        "local_oid_asc": lambda c: _none_high(c.local_oid),
        "local_oid_desc": lambda c: -_none_low(c.local_oid),
        "gi_asc": lambda c: _none_high(c.gi),
        "gi_desc": lambda c: -_none_low(c.gi),
        "volume_asc": lambda c: _none_high(c.volume),
        "volume_desc": lambda c: -_none_low(c.volume),
        "oid_asc": lambda c: _none_high(c.volume_oid),
        "oid_desc": lambda c: -_none_low(c.volume_oid),
        "length_asc": lambda c: _none_high(c.sequence_length),
        "length_desc": lambda c: -_none_low(c.sequence_length),
        "sstart_asc": lambda c: c.sstart,
        "sstart_desc": lambda c: -c.sstart,
        "accession_number_asc": lambda c: _accession_number(c.accession),
        "accession_number_desc": lambda c: -_accession_number(c.accession),
        "year_desc": lambda c: -_year(c.title),
    }
    strategies: list[tuple[str, SortKey]] = []
    items = list(features.items())
    for left_name, left_key in items:
        for right_name, right_key in items:
            if left_name == right_name:
                continue
            strategies.append(
                (
                    f"pair:{left_name}+{right_name}",
                    lambda candidate, lk=left_key, rk=right_key: (lk(candidate), rk(candidate), candidate.accession),
                )
            )
    return strategies


def _describe_population(candidates: list[Candidate], web_accessions: list[str]) -> dict[str, Any]:
    web_set = set(web_accessions)
    in_web = [candidate for candidate in candidates if candidate.accession in web_set]
    outside_web = [candidate for candidate in candidates if candidate.accession not in web_set]

    def numeric_summary(values: Iterable[int | float | None]) -> dict[str, Any]:
        concrete = [float(value) for value in values if value is not None]
        if not concrete:
            return {"count": 0}
        return {
            "count": len(concrete),
            "min": min(concrete),
            "median": statistics.median(concrete),
            "max": max(concrete),
        }

    return {
        "candidate_rows": len(candidates),
        "web_rows": len(web_accessions),
        "web_present_in_candidates": len(in_web),
        "score_classes": len(Counter((c.evalue, c.bits, c.score) for c in candidates)),
        "web_prefix2": Counter(_prefix(accession, 2) for accession in web_accessions).most_common(20),
        "candidate_prefix2": Counter(_prefix(candidate.accession, 2) for candidate in candidates).most_common(20),
        "web_numeric": {
            "local_oid": numeric_summary(candidate.local_oid for candidate in in_web),
            "gi": numeric_summary(candidate.gi for candidate in in_web),
            "volume": numeric_summary(candidate.volume for candidate in in_web),
            "volume_oid": numeric_summary(candidate.volume_oid for candidate in in_web),
            "sequence_length": numeric_summary(candidate.sequence_length for candidate in in_web),
        },
        "outside_web_numeric": {
            "local_oid": numeric_summary(candidate.local_oid for candidate in outside_web),
            "gi": numeric_summary(candidate.gi for candidate in outside_web),
            "volume": numeric_summary(candidate.volume for candidate in outside_web),
            "volume_oid": numeric_summary(candidate.volume_oid for candidate in outside_web),
            "sequence_length": numeric_summary(candidate.sequence_length for candidate in outside_web),
        },
    }


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# BLAST Tie-Order Inference Log",
        "",
        "This file is generated by `scripts/dev/infer-blast-tie-order.py`.",
        "It records failed and promising hypotheses for Web BLAST tied-hit order.",
        "",
        "## Population",
        "",
        f"- Candidate rows: {report['population']['candidate_rows']}",
        f"- Web rows: {report['population']['web_rows']}",
        f"- Web rows present in candidates: {report['population']['web_present_in_candidates']}",
        f"- Score classes: {report['population']['score_classes']}",
        "",
        "## Best Strategies",
        "",
        "| Rank | Strategy | Top10 | Top100 | Top500 | Same Top | Prefix |",
        "|---:|---|---:|---:|---:|---|---:|",
    ]
    for rank, item in enumerate(report["scores"][:25], start=1):
        lines.append(
            "| {rank} | `{name}` | {top10} | {top100} | {top500} | {same_top} | {prefix} |".format(
                rank=rank,
                name=item["name"],
                top10=item["top10_overlap"],
                top100=item["top100_overlap"],
                top500=item["top500_overlap"],
                same_top=item["same_top"],
                prefix=item["prefix_match_count"],
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Low overlap from accession, metadata, volume/OID, coordinate, title, and hash keys means the current evidence does not support a safe synthetic tie breaker.",
            "A strict Web-equivalent merge likely needs the original BLAST database subject order for the same database snapshot, or an explicit oracle produced from that snapshot.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--volume-oids", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()

    web_accessions = _read_web_accessions(args.web)
    candidates_by_accession = _read_candidates(args.candidates)
    candidates = _with_metadata(candidates_by_accession, args.metadata, args.volume_oids)
    strategies = _base_strategies(web_accessions) + _pair_strategies()
    scores = [_evaluate(name, candidates, key, web_accessions) for name, key in strategies]
    scores.sort(
        key=lambda item: (
            item["prefix_match_count"],
            item["same_top"],
            item["top10_overlap"],
            item["top100_overlap"],
            item["top500_overlap"],
        ),
        reverse=True,
    )
    report = {
        "inputs": {
            "web": str(args.web),
            "candidates": str(args.candidates),
            "metadata": str(args.metadata),
            "volume_oids": str(args.volume_oids),
        },
        "population": _describe_population(candidates, web_accessions),
        "scores": scores[: args.top],
        "strategy_count": len(strategies),
    }
    args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.markdown:
        _write_markdown(args.markdown, report)
    print(json.dumps({"strategy_count": len(strategies), "best": scores[:10]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
