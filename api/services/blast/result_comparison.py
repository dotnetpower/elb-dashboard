"""Build bounded, read-only comparisons between two BLAST result sets.

Responsibility: Read existing result blobs through the shared bounded helpers, aggregate HSPs
    by query/subject, and compute added, removed, changed, and unchanged hit identities.
Edit boundaries: No route/auth/state writes and no result artifact creation; callers supply
    authorized job/storage identities and own HTTP error shaping.
Key entry points: ``load_comparison_dataset``, ``compare_datasets``,
    ``ResultComparisonResponse``.
Risky contracts: Reads must respect existing file/byte/hit caps, partial inputs must remain
    explicitly partial, and output must never contain sequence strings or Storage URLs.
Validation: ``uv run pytest -q api/tests/test_blast_result_comparison.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from hashlib import sha256
from heapq import nsmallest
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.services.blast.result_analytics import (
    RESULTS_ALIGNMENTS_MAX_BYTES,
    RESULTS_ALIGNMENTS_MAX_HITS,
    RESULTS_MAX_FILES,
    ResultReadBudgetExceeded,
    coverage_percent,
    list_parseable_result_blobs,
    numeric_result_value,
    read_result_blob_texts_parallel,
)
from api.services.blast.results_parser import parse_blast_result_content
from api.services.sanitise import sanitise

_MAX_DIFF_ITEMS = 500
_MAX_IDENTIFIER_CHARS = 512
ComparisonStatus = Literal["added", "removed", "changed"]


class ComparisonReadError(RuntimeError):
    """No parseable result input could be read for a requested job."""


class HitComparison(BaseModel):
    """One added, removed, or materially changed query/subject hit."""

    query_id: str
    subject_id: str
    status: ComparisonStatus
    title: str | None = None
    organism: str | None = None
    before: dict[str, float | int | None] | None = None
    after: dict[str, float | int | None] | None = None


class ResultComparisonRequest(BaseModel):
    """Target job and bounded response size for a comparison."""

    against_job_id: str = Field(..., min_length=1, max_length=128)
    max_items: int = Field(default=200, ge=1, le=_MAX_DIFF_ITEMS)


class ResultComparisonResponse(BaseModel):
    """Versioned result-set comparison response."""

    schema_version: int = 1
    job_id: str
    against_job_id: str
    summary: dict[str, int | float]
    items: list[HitComparison]
    returned: int
    truncated: bool
    partial: bool
    inputs: dict[str, dict[str, int | bool]]


@dataclass(frozen=True)
class ComparisonDataset:
    """Aggregated hit identities plus bounded-read diagnostics for one job."""

    hits: dict[tuple[str, str], dict[str, Any]]
    total_hsps: int
    files_seen: int
    files_parsed: int
    read_failures: int
    truncated: bool


def _comparison_identifier(value: object, *, fallback: str = "") -> str:
    raw = str(value or fallback).strip()
    safe = sanitise(raw, mask_subscription_ids=False)
    if len(safe) <= _MAX_IDENTIFIER_CHARS and safe == raw:
        return safe
    digest = sha256(raw.encode("utf-8")).hexdigest()[:16]
    prefix = safe[: _MAX_IDENTIFIER_CHARS - len(digest) - 1]
    return f"{prefix}#{digest}"


def _aggregate_hit(bucket: dict[str, Any] | None, hit: dict[str, Any]) -> dict[str, Any]:
    evalue = numeric_result_value(hit.get("evalue"))
    bitscore = numeric_result_value(hit.get("bitscore"))
    identity = numeric_result_value(hit.get("pident"))
    query_cover = numeric_result_value(hit.get("qcovs"))
    if query_cover is None:
        query_cover = coverage_percent(
            hit.get("qstart"),
            hit.get("qend"),
            hit.get("qlen"),
            numeric_result_value(hit.get("length")),
        )
    if bucket is None:
        return {
            "evalue": evalue,
            "bitscore": bitscore,
            "identity": identity,
            "query_cover": query_cover,
            "hsp_count": 1,
            "title": sanitise(str(hit.get("stitle") or ""))[:240] or None,
            "organism": sanitise(str(hit.get("sscinames") or ""))[:160] or None,
        }
    bucket["hsp_count"] = int(bucket["hsp_count"]) + 1
    if evalue is not None and (bucket["evalue"] is None or evalue < bucket["evalue"]):
        bucket["evalue"] = evalue
    if bitscore is not None and (bucket["bitscore"] is None or bitscore > bucket["bitscore"]):
        bucket["bitscore"] = bitscore
    if identity is not None and (bucket["identity"] is None or identity > bucket["identity"]):
        bucket["identity"] = identity
    if query_cover is not None and (
        bucket["query_cover"] is None or query_cover > bucket["query_cover"]
    ):
        bucket["query_cover"] = query_cover
    return bucket


def load_comparison_dataset(job_id: str, storage_account: str) -> ComparisonDataset:
    """Read and aggregate one job's result set within existing analytics caps."""
    blobs = list_parseable_result_blobs(storage_account, job_id)
    if not blobs:
        raise ComparisonReadError(f"no parseable result file is available for {job_id}")
    selected_blobs = blobs[:RESULTS_MAX_FILES]
    reads = read_result_blob_texts_parallel(
        storage_account,
        selected_blobs,
        max_bytes=RESULTS_ALIGNMENTS_MAX_BYTES,
    )
    hits: dict[tuple[str, str], dict[str, Any]] = {}
    total_hsps = 0
    files_parsed = 0
    read_failures = 0
    truncated = len(blobs) > RESULTS_MAX_FILES
    for _path, content, read_error in reads:
        if isinstance(read_error, ResultReadBudgetExceeded):
            truncated = True
            continue
        if read_error is not None or content is None:
            read_failures += 1
            continue
        if len(content) >= RESULTS_ALIGNMENTS_MAX_BYTES - 4:
            truncated = True
        try:
            parsed = parse_blast_result_content(content)
        except Exception:
            read_failures += 1
            continue
        files_parsed += 1
        for hit in parsed:
            if total_hsps >= RESULTS_ALIGNMENTS_MAX_HITS:
                truncated = True
                break
            query_id = _comparison_identifier(hit.get("qseqid"), fallback="Query_1")
            subject_id = _comparison_identifier(hit.get("sseqid"))
            if not subject_id:
                continue
            key = (query_id, subject_id)
            hits[key] = _aggregate_hit(hits.get(key), hit)
            total_hsps += 1
        if total_hsps >= RESULTS_ALIGNMENTS_MAX_HITS:
            break
    if blobs and files_parsed == 0 and read_failures:
        raise ComparisonReadError(f"no result file could be read for {job_id}")
    return ComparisonDataset(
        hits=hits,
        total_hsps=total_hsps,
        files_seen=len(blobs),
        files_parsed=files_parsed,
        read_failures=read_failures,
        truncated=truncated,
    )


def _metrics(value: dict[str, Any]) -> dict[str, float | int | None]:
    return {
        "best_evalue": value.get("evalue"),
        "max_bitscore": value.get("bitscore"),
        "max_identity": value.get("identity"),
        "max_query_cover": value.get("query_cover"),
        "hsp_count": int(value.get("hsp_count") or 0),
    }


def _changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if int(before.get("hsp_count") or 0) != int(after.get("hsp_count") or 0):
        return True
    for key in ("bitscore", "identity", "query_cover"):
        left = before.get(key)
        right = after.get(key)
        if left is None or right is None:
            if left != right:
                return True
        elif not math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9):
            return True
    left_evalue = before.get("evalue")
    right_evalue = after.get("evalue")
    if left_evalue is None or right_evalue is None:
        return left_evalue != right_evalue
    return not math.isclose(
        float(left_evalue),
        float(right_evalue),
        rel_tol=1e-6,
        abs_tol=1e-300,
    )


def compare_datasets(
    *,
    job_id: str,
    against_job_id: str,
    current: ComparisonDataset,
    against: ComparisonDataset,
    max_items: int,
) -> ResultComparisonResponse:
    """Compare the current job against a selected baseline without mutating either input."""
    before = against
    after = current
    before_keys = set(before.hits)
    after_keys = set(after.hits)
    added_keys = after_keys - before_keys
    removed_keys = before_keys - after_keys
    common_keys = before_keys & after_keys
    changed_keys = {key for key in common_keys if _changed(before.hits[key], after.hits[key])}
    unchanged = len(common_keys) - len(changed_keys)

    items: list[HitComparison] = []
    limit = max(1, min(int(max_items), _MAX_DIFF_ITEMS))
    change_groups: tuple[
        tuple[ComparisonStatus, set[tuple[str, str]]],
        ...,
    ] = (
        ("added", added_keys),
        ("removed", removed_keys),
        ("changed", changed_keys),
    )
    for status, keys in change_groups:
        remaining = limit - len(items)
        if remaining <= 0:
            break
        for query_id, subject_id in nsmallest(remaining, keys):
            source = (
                before.hits[(query_id, subject_id)]
                if status == "removed"
                else after.hits[(query_id, subject_id)]
            )
            items.append(
                HitComparison(
                    query_id=_comparison_identifier(query_id, fallback="Query_1"),
                    subject_id=_comparison_identifier(subject_id),
                    status=status,
                    title=source.get("title"),
                    organism=source.get("organism"),
                    before=(
                        _metrics(before.hits[(query_id, subject_id)])
                        if (query_id, subject_id) in before.hits
                        else None
                    ),
                    after=(
                        _metrics(after.hits[(query_id, subject_id)])
                        if (query_id, subject_id) in after.hits
                        else None
                    ),
                )
            )
    total_changes = len(added_keys) + len(removed_keys) + len(changed_keys)
    union_count = len(before_keys | after_keys)
    return ResultComparisonResponse(
        job_id=job_id,
        against_job_id=against_job_id,
        summary={
            "before_hits": len(before_keys),
            "after_hits": len(after_keys),
            "common": len(common_keys),
            "unchanged": unchanged,
            "changed": len(changed_keys),
            "added": len(added_keys),
            "removed": len(removed_keys),
            "jaccard_percent": round((len(common_keys) / union_count) * 100, 2)
            if union_count
            else 100.0,
        },
        items=items,
        returned=min(total_changes, limit),
        truncated=total_changes > limit,
        partial=before.truncated
        or after.truncated
        or bool(before.read_failures or after.read_failures),
        inputs={
            job_id: {
                "files_seen": current.files_seen,
                "files_parsed": current.files_parsed,
                "read_failures": current.read_failures,
                "truncated": current.truncated,
            },
            against_job_id: {
                "files_seen": against.files_seen,
                "files_parsed": against.files_parsed,
                "read_failures": against.read_failures,
                "truncated": against.truncated,
            },
        },
    )
