"""BLAST result analytics and hit-filtering helpers.

Responsibility: BLAST result analytics and hit-filtering helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `InvalidResultBlobName`, `list_parseable_result_blobs`,
`_is_parseable_result_blob_name`, `validate_result_blob_name`, `numeric_result_value`,
`coverage_percent`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from api.services import get_credential, storage_data

LOGGER = logging.getLogger(__name__)

# Caps applied when parsing result blobs in the request thread. Real BLAST
# tabular files are kilobytes-to-low-megabytes; these caps prevent a
# pathologically large `-outfmt 7` from blowing the api sidecar memory if a
# user accidentally produced one with `-max_target_seqs 100000`.
RESULTS_MAX_FILES = 20
RESULTS_AGGREGATE_MAX_BYTES = 10 * 1024 * 1024
RESULTS_ALIGNMENTS_MAX_BYTES = 20 * 1024 * 1024
RESULTS_EXPORT_MAX_BYTES = 10 * 1024 * 1024
RESULTS_DEFAULT_PAGE_SIZE = 100
RESULTS_ALIGNMENTS_MAX_HITS = 50_000

# Cap how many rows we emit so the SPA never has to render a runaway map.
# A single BLAST query rarely matches more than a few thousand distinct
# subjects, so 5_000 is comfortably above the typical case and cheap to
# JSON-encode.
RESULTS_SUBJECT_AGGREGATE_LIMIT = 5_000
RESULTS_TAXONOMY_LIMIT = 2_000


class InvalidResultBlobName(ValueError):
    """Raised when a result blob name does not belong to the requested job."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code


def list_parseable_result_blobs(storage_account: str, job_id: str) -> list[dict[str, Any]]:
    """Return BLAST result blobs the analytics parser can understand."""
    cred = get_credential()
    blobs = storage_data.list_result_blobs(
        cred, storage_account, container="results", prefix=f"{job_id}/"
    )
    candidates = [
        blob
        for blob in blobs
        if isinstance(blob.get("name"), str) and _is_parseable_result_blob_name(blob["name"])
    ]
    merged = [
        blob
        for blob in candidates
        if _is_merged_result_blob_name(str(blob.get("name") or ""))
    ]
    return merged or candidates


def _is_parseable_result_blob_name(blob_name: str) -> bool:
    lower_name = blob_name.lower()
    parseable_suffixes = (".out", ".out.gz", ".xml", ".xml.gz")
    if not lower_name.endswith(parseable_suffixes):
        return False
    path_parts = set(lower_name.split("/"))
    if {"logs", "metadata"} & path_parts:
        return False
    return True


def _is_merged_result_blob_name(blob_name: str) -> bool:
    basename = blob_name.rsplit("/", 1)[-1].lower()
    return basename in {
        "merged_results.out",
        "merged_results.out.gz",
        "merged_results.xml",
        "merged_results.xml.gz",
    }


def validate_result_blob_name(blob_name: str, job_id: str) -> None:
    """Raise InvalidResultBlobName if the blob name escapes the job prefix."""
    if not job_id or "/" in job_id or ".." in job_id:
        raise InvalidResultBlobName("invalid_job_id")
    if not blob_name.startswith(f"{job_id}/"):
        raise InvalidResultBlobName("invalid_blob_name", "blob does not belong to this job")
    # Block path-traversal / URL-encoding tricks. Backslashes are treated as
    # separators by some Azure SDK layers, so reject them too.
    if ".." in blob_name or "?" in blob_name or "#" in blob_name or "\\" in blob_name:
        raise InvalidResultBlobName("invalid_blob_name")
    if "%2e" in blob_name.lower() or "%2f" in blob_name.lower():
        raise InvalidResultBlobName("invalid_blob_name")
    # Reject leading slash in the part after the prefix (defence in depth).
    remainder = blob_name[len(job_id) + 1 :]
    if remainder.startswith("/") or remainder == "":
        raise InvalidResultBlobName("invalid_blob_name")


def numeric_result_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None
    return None


def coverage_percent(
    start: Any,
    end: Any,
    total: Any,
    fallback_span: float | None = None,
) -> float | None:
    total_value = numeric_result_value(total)
    if total_value is None or total_value <= 0:
        return None
    start_value = numeric_result_value(start)
    end_value = numeric_result_value(end)
    if start_value is not None and end_value is not None:
        covered = abs(end_value - start_value) + 1
    elif fallback_span is not None:
        covered = fallback_span
    else:
        return None
    return round(max(0.0, min(100.0, (covered / total_value) * 100.0)), 1)


def annotate_result_hit(hit: dict[str, Any], source_blob: str | None = None) -> dict[str, Any]:
    annotated = dict(hit)
    if source_blob:
        annotated["source_blob"] = source_blob

    align_len = numeric_result_value(annotated.get("length"))
    query_cover = coverage_percent(
        annotated.get("qstart"), annotated.get("qend"), annotated.get("qlen"), align_len
    )
    subject_cover = coverage_percent(
        annotated.get("sstart"), annotated.get("send"), annotated.get("slen"), align_len
    )
    if query_cover is not None:
        annotated["qcovs"] = query_cover
    if subject_cover is not None:
        annotated["scovs"] = subject_cover

    identity = numeric_result_value(annotated.get("pident"))
    evalue = numeric_result_value(annotated.get("evalue"))
    query_cover = numeric_result_value(annotated.get("qcovs"))
    if identity is None or evalue is None or query_cover is None:
        annotated["review_status"] = "unclassified"
        annotated["review_reason"] = "Missing identity, e-value, or HSP query coverage."
    elif identity >= 99.5 and query_cover >= 95 and evalue <= 1e-20:
        annotated["review_status"] = "strong_match"
        annotated["review_reason"] = "Near-exact, high-coverage HSP."
    elif identity >= 95 and query_cover >= 80 and evalue <= 1e-5:
        annotated["review_status"] = "review_priority"
        annotated["review_reason"] = "High-similarity HSP worth diagnostic review."
    elif identity >= 90 and query_cover >= 50:
        annotated["review_status"] = "low_confidence"
        annotated["review_reason"] = "Moderate similarity or partial coverage."
    else:
        annotated["review_status"] = "weak_hit"
        annotated["review_reason"] = "Low similarity or short coverage."
    return annotated


def result_hit_matches_filters(
    hit: dict[str, Any],
    *,
    query_id: str,
    subject_id: str,
    organism: str,
    min_identity: float,
    min_bitscore: float,
    max_evalue: float,
    min_query_cover: float,
) -> bool:
    if query_id and hit.get("qseqid") != query_id:
        return False
    if subject_id and subject_id.lower() not in str(hit.get("sseqid", "")).lower():
        return False
    if organism:
        haystack = " ".join(
            str(hit.get(key, "")) for key in ("sscinames", "stitle", "sseqid", "staxids")
        ).lower()
        if organism.lower() not in haystack:
            return False
    identity = numeric_result_value(hit.get("pident"))
    if identity is not None and identity < min_identity:
        return False
    bitscore = numeric_result_value(hit.get("bitscore"))
    if bitscore is not None and bitscore < min_bitscore:
        return False
    evalue = numeric_result_value(hit.get("evalue"))
    if evalue is not None and evalue > max_evalue:
        return False
    query_cover = numeric_result_value(hit.get("qcovs"))
    if min_query_cover > 0 and (query_cover is None or query_cover < min_query_cover):
        return False
    return True


ResultRankAggregate = dict[str, float | None]


def _rank_bucket_key(hit: dict[str, Any]) -> tuple[str, str]:
    return (str(hit.get("qseqid") or ""), str(hit.get("sseqid") or ""))


def result_hit_rank_aggregates(
    hits: list[dict[str, Any]],
) -> dict[tuple[str, str], ResultRankAggregate]:
    """Build per-query/per-subject metrics used by NCBI-style result ranking."""
    buckets: dict[tuple[str, str], ResultRankAggregate] = {}
    for hit in hits:
        key = _rank_bucket_key(hit)
        row = buckets.setdefault(
            key,
            {
                "best_evalue": None,
                "max_bitscore": None,
                "total_bitscore": None,
                "max_query_cover": None,
                "max_identity": None,
                "max_length": None,
            },
        )
        evalue = numeric_result_value(hit.get("evalue"))
        if evalue is not None and (row["best_evalue"] is None or evalue < row["best_evalue"]):
            row["best_evalue"] = evalue
        bitscore = numeric_result_value(hit.get("bitscore"))
        if bitscore is not None:
            if row["max_bitscore"] is None or bitscore > row["max_bitscore"]:
                row["max_bitscore"] = bitscore
            row["total_bitscore"] = bitscore + (row["total_bitscore"] or 0.0)
        query_cover = numeric_result_value(hit.get("qcovs"))
        if query_cover is not None and (
            row["max_query_cover"] is None or query_cover > row["max_query_cover"]
        ):
            row["max_query_cover"] = query_cover
        identity = numeric_result_value(hit.get("pident"))
        if identity is not None and (row["max_identity"] is None or identity > row["max_identity"]):
            row["max_identity"] = identity
        length = numeric_result_value(hit.get("length"))
        if length is not None and (row["max_length"] is None or length > row["max_length"]):
            row["max_length"] = length
    return buckets


def _number_sort_key(value: float | None, *, high_first: bool) -> tuple[int, float]:
    if value is None:
        return (1, 0.0)
    return (0, -value if high_first else value)


def result_hit_sort_key(
    hit: dict[str, Any],
    sort_by: str,
    sort_dir: str,
    rank_aggregates: dict[tuple[str, str], ResultRankAggregate] | None = None,
) -> tuple[tuple[int, float], ...]:
    if sort_by == "relevance":
        aggregate = (rank_aggregates or {}).get(_rank_bucket_key(hit), {})
        best_first = sort_dir != "desc"
        return (
            _number_sort_key(aggregate.get("best_evalue"), high_first=not best_first),
            _number_sort_key(aggregate.get("max_bitscore"), high_first=best_first),
            _number_sort_key(aggregate.get("total_bitscore"), high_first=best_first),
            _number_sort_key(aggregate.get("max_query_cover"), high_first=best_first),
            _number_sort_key(aggregate.get("max_identity"), high_first=best_first),
            _number_sort_key(aggregate.get("max_length"), high_first=best_first),
        )
    value = numeric_result_value(hit.get(sort_by))
    return (_number_sort_key(value, high_first=sort_dir == "desc"),)


def rollup_subject_aggregates(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-subject `(sseqid -> {max_bitscore, total_bitscore, hsp_count})`."""
    bucket: dict[str, dict[str, Any]] = {}
    for hit in hits:
        sseqid = str(hit.get("sseqid") or "")
        if not sseqid:
            continue
        bitscore = numeric_result_value(hit.get("bitscore")) or 0.0
        existing = bucket.get(sseqid)
        if existing is None:
            bucket[sseqid] = {
                "sseqid": sseqid,
                "max_bitscore": bitscore,
                "total_bitscore": bitscore,
                "hsp_count": 1,
                "stitle": hit.get("stitle") or "",
                "sscinames": hit.get("sscinames") or "",
                "staxids": hit.get("staxids") or "",
            }
        else:
            existing["total_bitscore"] += bitscore
            if bitscore > existing["max_bitscore"]:
                existing["max_bitscore"] = bitscore
            existing["hsp_count"] += 1
    rows = sorted(
        bucket.values(),
        key=lambda row: (-row["total_bitscore"], -row["max_bitscore"], row["sseqid"]),
    )
    return rows[:RESULTS_SUBJECT_AGGREGATE_LIMIT]


def rollup_taxonomy(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-organism rollup `(sscinames|taxid -> {hits, best_evalue, top_bitscore})`."""
    bucket: dict[str, dict[str, Any]] = {}
    for hit in hits:
        organism = str(hit.get("sscinames") or "").split(";")[0].strip()
        taxid = str(hit.get("staxids") or "").split(";")[0].strip()
        key = (organism or taxid or "unclassified").lower()
        evalue = numeric_result_value(hit.get("evalue"))
        bitscore = numeric_result_value(hit.get("bitscore"))
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = {
                "key": key,
                "organism": organism,
                "taxid": taxid,
                "count": 1,
                "best_evalue": evalue,
                "top_bitscore": bitscore,
            }
        else:
            existing["count"] += 1
            if evalue is not None and (
                existing["best_evalue"] is None or evalue < existing["best_evalue"]
            ):
                existing["best_evalue"] = evalue
            if bitscore is not None and (
                existing["top_bitscore"] is None or bitscore > existing["top_bitscore"]
            ):
                existing["top_bitscore"] = bitscore
    rows = sorted(
        bucket.values(),
        key=lambda row: (-row["count"], row["organism"] or row["taxid"]),
    )
    return rows[:RESULTS_TAXONOMY_LIMIT]


def enrich_taxonomy_with_lineage(
    rows: list[dict[str, Any]], *, taxid_limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fill in NCBI lineage chains for the top-N taxa, in-place."""
    from api.services.taxonomy import TaxonomySearchUnavailable, fetch_taxonomy_detail

    looked_up = 0
    failed = 0
    limit_reached = 0
    for index, row in enumerate(rows):
        taxid_str = str(row.get("taxid") or "").strip()
        if not taxid_str:
            continue
        try:
            taxid_int = int(taxid_str)
        except ValueError:
            continue
        if index >= taxid_limit:
            limit_reached += 1
            continue
        try:
            detail = fetch_taxonomy_detail(taxid_int)
        except TaxonomySearchUnavailable:
            failed += 1
            continue
        except Exception as exc:
            failed += 1
            LOGGER.warning(
                "taxonomy lineage: detail for taxid=%s failed: %s",
                taxid_int,
                type(exc).__name__,
            )
            continue
        row["lineage"] = str(detail.get("lineage") or "")
        row["lineage_ex"] = list(detail.get("lineage_ex") or [])
        looked_up += 1
    meta = {
        "requested": True,
        "looked_up": looked_up,
        "failed": failed,
        "limit_reached": limit_reached,
    }
    return rows, meta
