"""Background builders for BLAST result UI artifacts.

Responsibility: Background builders for BLAST result UI artifacts
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_number`, `_hit_is_better`, `_StreamingAggregate`,
`build_result_manifest_payload`, `build_result_aggregate_payload`,
`build_default_alignments_payload`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from api.services import get_credential, storage_data
from api.services.blast_result_analytics import (
    RESULTS_AGGREGATE_MAX_BYTES,
    RESULTS_ALIGNMENTS_MAX_BYTES,
    RESULTS_ALIGNMENTS_MAX_HITS,
    RESULTS_DEFAULT_PAGE_SIZE,
    RESULTS_MAX_FILES,
    annotate_result_hit,
    enrich_taxonomy_with_lineage,
    list_parseable_result_blobs,
    result_hit_matches_filters,
    result_hit_rank_aggregates,
    result_hit_sort_key,
    rollup_subject_aggregates,
    rollup_taxonomy,
)
from api.services.blast_result_manifest import build_result_manifest
from api.services.blast_results_parser import parse_blast_result_content
from api.services.job_artifacts import upsert_artifact_state, write_result_analytics_artifact

LOGGER = logging.getLogger(__name__)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _hit_is_better(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    cand_e = _number(candidate.get("evalue"))
    curr_e = _number(current.get("evalue"))
    cand_b = _number(candidate.get("bitscore"))
    curr_b = _number(current.get("bitscore"))
    cand_e = cand_e if cand_e is not None else float("inf")
    curr_e = curr_e if curr_e is not None else float("inf")
    if cand_e != curr_e:
        return cand_e < curr_e
    cand_b = cand_b if cand_b is not None else float("-inf")
    curr_b = curr_b if curr_b is not None else float("-inf")
    return cand_b > curr_b


class _StreamingAggregate:
    def __init__(self) -> None:
        self.total = 0
        self.unique_queries: set[str] = set()
        self.unique_subjects: set[str] = set()
        self.subject_counts: dict[str, int] = {}
        self.best_per_query: dict[str, dict[str, Any]] = {}
        self.evalue_bins = {
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
        self.identity_bins = {f"{p}-{p + 10}%": 0 for p in range(0, 100, 10)}
        self.identity_bins["100%"] = 0
        self.identity_sum = 0.0
        self.identity_count = 0
        self.bitscore_sum = 0.0
        self.bitscore_count = 0
        self.length_sum = 0.0
        self.length_count = 0
        self.max_bitscore: float | None = None
        self.min_evalue: float | None = None

    def add(self, hit: dict[str, Any]) -> None:
        self.total += 1
        qid = str(hit.get("qseqid", "") or "")
        sid = str(hit.get("sseqid", "") or "")
        if qid:
            self.unique_queries.add(qid)
            previous = self.best_per_query.get(qid)
            if previous is None or _hit_is_better(hit, previous):
                self.best_per_query[qid] = hit
        if sid:
            self.unique_subjects.add(sid)
            self.subject_counts[sid] = self.subject_counts.get(sid, 0) + 1
        evalue = _number(hit.get("evalue"))
        if evalue is not None and evalue >= 0:
            self.min_evalue = evalue if self.min_evalue is None else min(self.min_evalue, evalue)
            if evalue == 0:
                self.evalue_bins["0"] += 1
            elif evalue < 1e-100:
                self.evalue_bins["1e-200..1e-100"] += 1
            elif evalue < 1e-50:
                self.evalue_bins["1e-100..1e-50"] += 1
            elif evalue < 1e-10:
                self.evalue_bins["1e-50..1e-10"] += 1
            elif evalue < 1e-5:
                self.evalue_bins["1e-10..1e-5"] += 1
            elif evalue < 0.01:
                self.evalue_bins["1e-5..0.01"] += 1
            elif evalue < 1:
                self.evalue_bins["0.01..1"] += 1
            elif evalue <= 10:
                self.evalue_bins["1..10"] += 1
            else:
                self.evalue_bins[">10"] += 1
        identity = _number(hit.get("pident"))
        if identity is not None:
            self.identity_sum += identity
            self.identity_count += 1
            if identity == 100:
                self.identity_bins["100%"] += 1
            else:
                bucket = max(0, min(90, int(identity // 10) * 10))
                self.identity_bins[f"{bucket}-{bucket + 10}%"] += 1
        bitscore = _number(hit.get("bitscore"))
        if bitscore is not None:
            self.bitscore_sum += bitscore
            self.bitscore_count += 1
            self.max_bitscore = (
                bitscore if self.max_bitscore is None else max(self.max_bitscore, bitscore)
            )
        length = hit.get("length")
        if isinstance(length, int):
            self.length_sum += float(length)
            self.length_count += 1

    def as_stats(self) -> dict[str, Any]:
        top_subjects = sorted(self.subject_counts.items(), key=lambda item: item[1], reverse=True)[
            :20
        ]
        top_hit_per_query = [
            {
                "qseqid": qid,
                "sseqid": str(hit.get("sseqid", "") or ""),
                "pident": _number(hit.get("pident")),
                "evalue": _number(hit.get("evalue")),
                "bitscore": _number(hit.get("bitscore")),
                "length": hit.get("length") if isinstance(hit.get("length"), int) else None,
                "stitle": str(hit.get("stitle", "") or "") or None,
            }
            for qid, hit in sorted(self.best_per_query.items())
        ]
        return {
            "total_hits": self.total,
            "unique_queries": len(self.unique_queries),
            "unique_subjects": len(self.unique_subjects),
            "evalue_distribution": self.evalue_bins,
            "identity_distribution": self.identity_bins,
            "top_subjects": [{"id": sid, "count": count} for sid, count in top_subjects],
            "top_hit_per_query": top_hit_per_query,
            "avg_identity": round(self.identity_sum / self.identity_count, 2)
            if self.identity_count
            else None,
            "avg_bitscore": round(self.bitscore_sum / self.bitscore_count, 2)
            if self.bitscore_count
            else None,
            "avg_length": round(self.length_sum / self.length_count, 1)
            if self.length_count
            else None,
            "max_bitscore": self.max_bitscore,
            "min_evalue": self.min_evalue,
        }


def build_result_manifest_payload(job_id: str, storage_account: str) -> dict[str, Any]:
    files = storage_data.list_result_blobs(
        get_credential(), storage_account, container="results", prefix=job_id
    )
    return {
        "job_id": job_id,
        "files": files,
        "results": files,
        "manifest": build_result_manifest(job_id=job_id, files=files),
    }


def _read_hits(
    *,
    job_id: str,
    storage_account: str,
    max_bytes: int,
    max_hits: int | None = None,
) -> dict[str, Any]:
    cred = get_credential()
    result_blobs = list_parseable_result_blobs(storage_account, job_id)
    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
    hit_limit_reached = False
    blob_names: list[str] = []
    for blob_info in result_blobs[:RESULTS_MAX_FILES]:
        if max_hits is not None and len(all_hits) >= max_hits:
            hit_limit_reached = True
            break
        blob_path = str(blob_info.get("name") or "")
        if not blob_path:
            continue
        try:
            content = storage_data.read_result_blob_text(
                cred,
                storage_account,
                "results",
                blob_path,
                max_bytes=max_bytes,
            )
            parsed_hits = parse_blast_result_content(content)
            if max_hits is None:
                selected = parsed_hits
            else:
                remaining = max_hits - len(all_hits)
                selected = parsed_hits[:remaining]
                if len(parsed_hits) > remaining:
                    hit_limit_reached = True
            all_hits.extend(annotate_result_hit(hit, blob_path) for hit in selected)
            parsed_files += 1
            blob_names.append(blob_path)
            if hit_limit_reached:
                break
        except Exception as exc:
            read_failures += 1
            LOGGER.warning(
                "result artifact builder failed to parse %s for %s: %s",
                blob_path,
                job_id,
                type(exc).__name__,
            )
    return {
        "hits": all_hits,
        "blob_names": blob_names,
        "files_parsed": parsed_files,
        "total_files": len(result_blobs),
        "read_failures": read_failures,
        "truncated": len(result_blobs) > RESULTS_MAX_FILES or hit_limit_reached,
        "hit_limit_reached": hit_limit_reached,
    }


def build_result_aggregate_payload(job_id: str, storage_account: str) -> dict[str, Any]:
    cred = get_credential()
    result_blobs = list_parseable_result_blobs(storage_account, job_id)
    aggregate = _StreamingAggregate()
    parsed_files = 0
    read_failures = 0
    for blob_info in result_blobs[:RESULTS_MAX_FILES]:
        try:
            content = storage_data.read_result_blob_text(
                cred,
                storage_account,
                "results",
                str(blob_info["name"]),
                max_bytes=RESULTS_AGGREGATE_MAX_BYTES,
            )
            for hit in parse_blast_result_content(content):
                aggregate.add(hit)
            parsed_files += 1
        except Exception as exc:
            read_failures += 1
            LOGGER.warning(
                "aggregate artifact builder failed to parse %s for %s: %s",
                blob_info.get("name"),
                job_id,
                type(exc).__name__,
            )
    if aggregate.total == 0 and parsed_files == 0 and read_failures:
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "all_reads_failed",
            "stats": None,
            "files_parsed": 0,
            "total_files": len(result_blobs),
            "read_failures": read_failures,
        }
    return {
        "job_id": job_id,
        "status": "ok" if aggregate.total else "no_hits",
        "stats": aggregate.as_stats(),
        "files_parsed": parsed_files,
        "total_files": len(result_blobs),
        "read_failures": read_failures,
        "truncated": len(result_blobs) > RESULTS_MAX_FILES,
    }


def build_default_alignments_payload(job_id: str, storage_account: str) -> dict[str, Any]:
    read = _read_hits(
        job_id=job_id,
        storage_account=storage_account,
        max_bytes=RESULTS_ALIGNMENTS_MAX_BYTES,
        max_hits=RESULTS_ALIGNMENTS_MAX_HITS,
    )
    all_hits = list(read["hits"])
    filtered = [
        hit
        for hit in all_hits
        if result_hit_matches_filters(
            hit,
            query_id="",
            subject_id="",
            organism="",
            min_identity=0.0,
            min_bitscore=0.0,
            max_evalue=10.0,
            min_query_cover=0.0,
        )
    ]
    rank_aggregates = result_hit_rank_aggregates(filtered)
    filtered.sort(key=lambda hit: result_hit_sort_key(hit, "relevance", "asc", rank_aggregates))
    page_size = RESULTS_DEFAULT_PAGE_SIZE
    page_hits = filtered[:page_size]
    page_count = (len(filtered) + page_size - 1) // page_size
    return {
        "artifact_schema_version": 2,
        "job_id": job_id,
        "blob_name": read["blob_names"][0] if len(read["blob_names"]) == 1 else "",
        "blob_names": read["blob_names"],
        "alignments": page_hits,
        "total_hits": len(all_hits),
        "filtered_hits": len(filtered),
        "returned": len(page_hits),
        "query_ids": sorted({str(h.get("qseqid", "")) for h in all_hits if h.get("qseqid")})[:200],
        "subject_aggregates": rollup_subject_aggregates(filtered),
        "page": 1,
        "page_size": page_size,
        "pages": page_count,
        "files_parsed": read["files_parsed"],
        "total_files": read["total_files"],
        "read_failures": read["read_failures"],
        "truncated": read["truncated"],
        "hit_limit_reached": read["hit_limit_reached"],
        "filters": {
            "query_id": None,
            "subject_id": None,
            "organism": None,
            "min_identity": None,
            "min_bitscore": None,
            "max_evalue": 10.0,
            "min_query_cover": None,
            "sort_by": "relevance",
            "sort_dir": "asc",
        },
    }


def build_default_taxonomy_payload(job_id: str, storage_account: str) -> dict[str, Any]:
    read = _read_hits(
        job_id=job_id,
        storage_account=storage_account,
        max_bytes=RESULTS_ALIGNMENTS_MAX_BYTES,
        max_hits=RESULTS_ALIGNMENTS_MAX_HITS,
    )
    hits = list(read["hits"])
    organisms = rollup_taxonomy(hits)
    lineage_meta: dict[str, Any] = {
        "requested": True,
        "looked_up": 0,
        "name_resolved": 0,
        "failed": 0,
    }
    # Bake lineage / blast_name once on the worker so the SPA's default
    # Taxonomy open (which always sends `include_lineage=true`) can serve
    # the artifact and skip the eutils round-trip on the request thread.
    # The taxid_limit mirrors the route default (20) — only top-20
    # organisms get the NCBI lookup, which is cheap (cached) and bounded.
    if organisms:
        try:
            organisms, lineage_meta = enrich_taxonomy_with_lineage(
                organisms, taxid_limit=20
            )
        except Exception as exc:
            # Lineage enrichment is best-effort; never fail the artifact
            # bake because of an upstream eutils hiccup.
            LOGGER.info(
                "default taxonomy artifact: lineage enrichment skipped job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
    return {
        "artifact_schema_version": 2,
        "job_id": job_id,
        "organisms": organisms,
        "total_hits": len(hits),
        "filtered_hits": len(hits),
        "files_parsed": read["files_parsed"],
        "total_files": read["total_files"],
        "read_failures": read["read_failures"],
        "truncated": read["truncated"],
        "lineage": lineage_meta,
    }


def build_and_write_default_result_artifacts(job_id: str, storage_account: str) -> dict[str, Any]:
    """Build small default result artifacts for fast page reopen."""
    written: dict[str, str] = {}
    builders = [
        ("result_manifest", build_result_manifest_payload),
        ("result_aggregate", build_result_aggregate_payload),
        ("result_alignments", build_default_alignments_payload),
        ("result_taxonomy", build_default_taxonomy_payload),
    ]
    for artifact_type, builder in builders:
        try:
            payload = builder(job_id, storage_account)
            state = write_result_analytics_artifact(job_id, artifact_type, payload)
            written[artifact_type] = state.blob_path
        except Exception as exc:
            LOGGER.warning(
                "result artifact build failed job_id=%s type=%s: %s",
                job_id,
                artifact_type,
                type(exc).__name__,
            )
            try:
                upsert_artifact_state(
                    job_id,
                    artifact_type,
                    status="failed",
                    error_code=type(exc).__name__,
                )
            except Exception:
                LOGGER.debug("result artifact failure state write failed", exc_info=True)
    return {"job_id": job_id, "written": written}
