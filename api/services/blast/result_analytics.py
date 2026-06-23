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
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from api.services import get_credential
from api.services.storage import data as storage_data

LOGGER = logging.getLogger(__name__)

# Upper bound on concurrent Storage reads when fanning out result-blob parsing.
# Result sets are capped at ``RESULTS_MAX_FILES`` (20); reading them one HTTP
# roundtrip at a time serialises export/aggregate latency, while an unbounded
# fan-out would open one connection per blob. Eight keeps the wall-clock win
# without exhausting the thread-local blob-client pool.
_RESULT_READ_MAX_WORKERS = 8

# Tokens after which the remainder of a BLAST subject title (`stitle`) is no
# longer the scientific name. NCBI titles look like
# "Monkeypox virus isolate 24MPX2634V genome assembly, complete genome",
# "Homo sapiens chromosome 7, GRCh38 reference",
# "Escherichia coli strain K-12 complete genome".
# We cut at the first occurrence (case-insensitive, word-boundary) of any
# of these so the prefix is the scientific name candidate.
_STITLE_ORGANISM_STOPWORDS = (
    "isolate",
    "strain",
    "clone",
    "chromosome",
    "complete",
    "partial",
    "genome",
    "sequence",
    "scaffold",
    "contig",
    "plasmid",
    "mitochond",
    "chloroplast",
    "segment",
    "cds",
    "mRNA",
    "rRNA",
    "tRNA",
    "ncRNA",
    "gene",
    "BAC",
)

# Curator/source qualifier prefixes NCBI prepends to the title. These are
# stripped first, before we look for the organism cut-off.
_STITLE_LEADING_QUALIFIERS = (
    "PREDICTED:",
    "TPA:",
    "TPA_inf:",
    "UNVERIFIED:",
    "MAG:",
    "PARTIAL:",
    "LOW QUALITY PROTEIN:",
    "RecName:",
)

_STITLE_STOP_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in _STITLE_ORGANISM_STOPWORDS) + r")\b",
    re.IGNORECASE,
)


def extract_organism_from_stitle(stitle: str) -> str:
    """Best-effort organism name extracted from a BLAST subject title.

    Used as a fallback when the BLAST output lacks `sscinames`/`staxids`
    columns (default `-outfmt 6` does not include them). Returns "" when no
    confident candidate exists so callers can keep the `unclassified`
    bucket they had before.
    """
    if not stitle:
        return ""
    text = str(stitle).strip()
    if not text:
        return ""
    # Strip leading record-id annotations like "gi|123|gb|X|" or
    # ">accession ", which sometimes leak into stitle when the parser sees
    # an unparsed header.
    if "|" in text and text.startswith(("gi|", "ref|", "gb|", "emb|", "dbj|", "sp|", "tr|")):
        text = text.split(" ", 1)[1] if " " in text else ""
    text = text.lstrip(">").strip()
    # Strip curator qualifier prefixes (case-insensitive). Loop so chained
    # qualifiers like "TPA_inf: PREDICTED: Foo" are all removed.
    changed = True
    while changed and text:
        changed = False
        for qualifier in _STITLE_LEADING_QUALIFIERS:
            if text[: len(qualifier)].upper() == qualifier.upper():
                text = text[len(qualifier) :].strip()
                changed = True
                break
    if not text:
        return ""
    # Cut at the first stop-word; fall back to the first comma. Also drop
    # any trailing parenthesised qualifier ("(LOC123)").
    stop_match = _STITLE_STOP_RE.search(text)
    cutoff = stop_match.start() if stop_match else len(text)
    comma = text.find(",")
    if 0 <= comma < cutoff:
        cutoff = comma
    paren = text.find("(")
    if 0 <= paren < cutoff:
        cutoff = paren
    candidate = text[:cutoff].strip(" ,.:;-")
    # Drop overly long candidates — anything past 6 tokens is almost
    # certainly not a clean scientific name and we'd rather show
    # "unclassified" than mislabel a row.
    tokens = candidate.split()
    if not (1 <= len(tokens) <= 6):
        return ""
    # Single-letter or numeric-only first token is junk.
    if len(tokens[0]) < 2 or tokens[0].isdigit():
        return ""
    return " ".join(tokens)

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


def has_blast_success_marker(storage_account: str, job_id: str) -> bool:
    """True when the durable elastic-blast SUCCESS marker exists for a job.

    The cluster-side finalizer (``elb-finalizer-aks.sh``) writes
    ``.../metadata/SUCCESS.txt`` LAST — only after every result artifact has
    been durably uploaded to the results container — so its presence is the
    authoritative "this job completed successfully" signal. Unlike the AKS
    cluster (which auto-stop / delete can tear down right after a job finishes)
    and the ephemeral Celery result / per-revision Redis runtime cache, the
    marker lives in Storage and survives those teardowns. It is therefore the
    correct ground truth for the stale-job reconciler to consult before
    declaring a quiet, otherwise-unreachable job ``worker_lost``.

    Best-effort: returns ``False`` on any error (missing account/job id,
    credential failure, blob list failure) so a transient Storage hiccup never
    falsely completes a job. ``job_id`` is the results-container prefix — the
    dashboard job id for Celery-submitted jobs, or the sibling OpenAPI job id
    for ``/v1/jobs`` submissions (the runner stores results under
    ``results/<job_id>/...`` in both cases), mirroring
    ``api.services.blast.runtime_failure.read_blast_runtime_failure``.
    """
    if not storage_account or not job_id:
        return False
    try:
        from api.services.storage.job_prefix import resolve_results_prefix

        cred = get_credential()
        blobs = storage_data.list_result_blobs(
            cred, storage_account, container="results", prefix=resolve_results_prefix(job_id)
        )
    except Exception as exc:
        LOGGER.info(
            "success marker check skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        return False
    return any(
        str(blob.get("name") or "").endswith("/metadata/SUCCESS.txt") for blob in blobs
    )


def list_parseable_result_blobs(
    storage_account: str, job_id: str, *, prefix: str | None = None
) -> list[dict[str, Any]]:
    """Return BLAST result blobs the analytics parser can understand.

    ``prefix`` overrides the results-container prefix (issue #67 threads the
    stored, possibly date-tiered, prefix here); when None it falls back to the
    legacy ``{job_id}/`` layout via the resolver.
    """
    from api.services.storage.job_prefix import resolve_results_prefix

    cred = get_credential()
    blobs = storage_data.list_result_blobs(
        cred,
        storage_account,
        container="results",
        prefix=prefix or resolve_results_prefix(job_id),
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


def read_result_blob_texts_parallel(
    storage_account: str,
    blob_infos: Sequence[dict[str, Any]],
    *,
    max_bytes: int,
    max_workers: int = _RESULT_READ_MAX_WORKERS,
) -> list[tuple[str, str | None, BaseException | None]]:
    """Read result blobs concurrently, preserving the input order.

    Returns one ``(blob_path, content, error)`` tuple per input blob, in the
    same order as ``blob_infos``. For a successful read ``content`` is the text
    and ``error`` is ``None``; for a failed read ``content`` is ``None`` and
    ``error`` carries the exception so callers keep their existing
    per-blob failure accounting. A blob whose name is empty yields
    ``(``"``, None, None)`` so callers can skip it exactly as the previous
    serial loops did.

    Blob-service clients are thread-local and pooled, so concurrent reads are
    safe; concurrency is bounded by ``max_workers`` so a 20-file job never opens
    more than that many simultaneous Storage connections.
    """
    cred = get_credential()
    paths = [str(info.get("name") or "") for info in blob_infos]

    def _read(path: str) -> tuple[str, str | None, BaseException | None]:
        if not path:
            return ("", None, None)
        try:
            content = storage_data.read_result_blob_text(
                cred,
                storage_account,
                "results",
                path,
                max_bytes=max_bytes,
            )
            return (path, content, None)
        except Exception as exc:  # per-blob failure captured, not fatal
            return (path, None, exc)

    if len(paths) <= 1:
        return [_read(path) for path in paths]

    workers = min(max_workers, len(paths))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="blast-result-read"
    ) as executor:
        return list(executor.map(_read, paths))


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
    # Prefer a qcovs value the run reported directly (BLAST's
    # "% query coverage per subject" column = NCBI Web BLAST's Query Cover):
    # only fall back to the per-HSP coordinate-derived estimate when the
    # tabular / XML output did not carry qcovs, so a real reported value is
    # never clobbered by the weaker computed one.
    if numeric_result_value(annotated.get("qcovs")) is None and query_cover is not None:
        annotated["qcovs"] = query_cover
    if numeric_result_value(annotated.get("scovs")) is None and subject_cover is not None:
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
    """Per-organism rollup `(sscinames|taxid -> {hits, best_evalue, top_bitscore})`.

    When `sscinames`/`staxids` are absent (default `-outfmt 6` and most
    `-outfmt 5` exports do not include them) the rollup falls back to
    extracting a candidate organism name from `stitle`. The fallback is
    marked `organism_source: "stitle"` so the API consumer can resolve the
    name to a taxid (eutils) or display a "best-effort" indicator.
    """
    bucket: dict[str, dict[str, Any]] = {}
    for hit in hits:
        organism = str(hit.get("sscinames") or "").split(";")[0].strip()
        taxid = str(hit.get("staxids") or "").split(";")[0].strip()
        organism_source = "sscinames" if organism else ""
        if not organism and not taxid:
            fallback = extract_organism_from_stitle(str(hit.get("stitle") or ""))
            if fallback:
                organism = fallback
                organism_source = "stitle"
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
                "organism_source": organism_source,
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
            # Prefer the strongest source we've seen for this bucket.
            if organism_source == "sscinames" and existing.get("organism_source") != "sscinames":
                existing["organism_source"] = "sscinames"
                if organism:
                    existing["organism"] = organism
    rows = sorted(
        bucket.values(),
        key=lambda row: (-row["count"], row["organism"] or row["taxid"]),
    )
    return rows[:RESULTS_TAXONOMY_LIMIT]


def enrich_taxonomy_with_lineage(
    rows: list[dict[str, Any]], *, taxid_limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fill in NCBI lineage chains for the top-N taxa, in-place.

    For rows that came from the `stitle` fallback (no `staxids` in the
    BLAST output) we first resolve the organism name to a taxid via
    `search_taxonomy` so the lineage / blast_name lookup can proceed.
    Both the name→taxid resolution and the per-taxid detail call are
    cached in the `taxonomy` service module-level caches.

    Each enriched row gains:
      * `lineage`     — semicolon-joined NCBI lineage string.
      * `lineage_ex`  — parsed lineage chain (root → leaf).
      * `blast_name`  — top-level NCBI group (`Viruses`, `Bacteria`,
                        `Mammals`, …), matching NCBI's BLAST UI column.
    """
    from api.services.taxonomy import (
        TaxonomySearchUnavailable,
        fetch_taxonomy_detail,
        search_taxonomy,
    )

    looked_up = 0
    name_resolved = 0
    failed = 0
    limit_reached = 0
    for index, row in enumerate(rows):
        if index >= taxid_limit:
            limit_reached += 1
            continue
        taxid_str = str(row.get("taxid") or "").strip()
        if not taxid_str:
            organism = str(row.get("organism") or "").strip()
            if not organism:
                continue
            try:
                payload = search_taxonomy(organism, limit=1)
            except TaxonomySearchUnavailable:
                failed += 1
                continue
            except ValueError:
                # Organism name rejected by validator (blank, too long).
                continue
            except Exception as exc:
                failed += 1
                LOGGER.warning(
                    "taxonomy lineage: name->taxid for %r failed: %s",
                    organism,
                    type(exc).__name__,
                )
                continue
            candidates = payload.get("results") or []
            if not candidates:
                continue
            resolved = candidates[0].get("taxid")
            if not isinstance(resolved, int) or resolved <= 0:
                continue
            taxid_str = str(resolved)
            row["taxid"] = taxid_str
            row["taxid_source"] = "name_lookup"
            name_resolved += 1
        try:
            taxid_int = int(taxid_str)
        except ValueError:
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
        blast_name = _blast_group_from_lineage(row["lineage"], row["lineage_ex"])
        if blast_name:
            row["blast_name"] = blast_name
        # Fill in the scientific name from NCBI if the heuristic produced
        # something different (NCBI is authoritative once we have a taxid).
        scientific = str(detail.get("scientific_name") or "").strip()
        if scientific and scientific.lower() != "taxid " + str(taxid_int):
            row["organism"] = scientific
        looked_up += 1
    meta = {
        "requested": True,
        "looked_up": looked_up,
        "name_resolved": name_resolved,
        "failed": failed,
        "limit_reached": limit_reached,
    }
    return rows, meta


# Top-level lineage roots that map to NCBI's BLAST "Name" column. The
# matcher walks the lineage in NCBI order (root → leaf) and stops at the
# first one it recognises so deep lineages don't accidentally collapse
# into "cellular organisms".
_BLAST_GROUP_BY_ROOT_TOKEN: tuple[tuple[str, str], ...] = (
    ("Viruses", "viruses"),
    ("Viroids", "viroids"),
    ("Bacteria", "bacteria"),
    ("Archaea", "archaea"),
    ("Mammalia", "mammals"),
    ("Aves", "birds"),
    ("Reptilia", "reptiles"),
    ("Amphibia", "amphibians"),
    ("Actinopterygii", "bony fishes"),
    ("Chondrichthyes", "cartilaginous fishes"),
    ("Insecta", "insects"),
    ("Arachnida", "arachnids"),
    ("Crustacea", "crustaceans"),
    ("Nematoda", "nematodes"),
    ("Mollusca", "molluscs"),
    ("Platyhelminthes", "flatworms"),
    ("Cnidaria", "cnidarians"),
    ("Echinodermata", "echinoderms"),
    ("Embryophyta", "plants"),
    ("Streptophyta", "plants"),
    ("Viridiplantae", "plants"),
    ("Fungi", "fungi"),
    ("Eukaryota", "eukaryotes"),
)


def _blast_group_from_lineage(
    lineage_str: str, lineage_ex: list[dict[str, Any]]
) -> str | None:
    """Return the NCBI BLAST "Name" group for a lineage chain."""
    chain_names: list[str] = []
    for node in lineage_ex or ():
        name = str(node.get("scientific_name") or "").strip()
        if name:
            chain_names.append(name)
    if not chain_names and lineage_str:
        chain_names = [token.strip() for token in lineage_str.split(";") if token.strip()]
    # Iterate markers in specificity order (most specific first) so the
    # finest matching clade wins — "Mammalia" must beat "Eukaryota" for
    # Homo sapiens, etc.
    chain_lower = {name.lower() for name in chain_names}
    for marker, label in _BLAST_GROUP_BY_ROOT_TOKEN:
        if marker.lower() in chain_lower:
            return label
    return None
