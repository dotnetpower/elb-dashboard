"""BLAST submit payload normalization helpers.

Responsibility: BLAST submit payload normalization helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `canonical_submit_metadata`, `canonical_submit_snapshot`,
`canonical_execution_config`, `submit_contracts`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from fastapi import HTTPException

_BLAST_SUBMIT_OPTION_KEYS = frozenset(
    {
        "additional_options",
        "acr_name",
        "acr_resource_group",
        "allow_approximate_sharding",
        "batch_len",
        "best_hit_overhang",
        "best_hit_score_edge",
        "comp_based_stats",
        "culling_limit",
        "db_auto_partition",
        "db_effective_search_space",
        "db_partitions",
        "db_partition_prefix",
        "db_sharded",
        "db_total_bytes",
        "db_total_letters",
        "disable_sharding",
        "enable_warmup",
        "evalue",
        "gap_extend",
        "gap_open",
        "gilist",
        "is_inclusive",
        "lcase_masking",
        "low_complexity_filter",
        "machine_type",
        "matrix",
        "max_target_seqs",
        "mem_limit",
        "mem_request",
        "negative_gilist",
        "num_alignments",
        "num_descriptions",
        "num_nodes",
        "outfmt",
        "parse_deflines",
        "pd_size",
        "perc_identity",
        "qcov_hsp_perc",
        "query_count",
        "query_effective_search_spaces",
        "reuse",
        "seqidlist",
        "shard_sets",
        "sharding_mode",
        "soft_masking",
        "taxid",
        "threshold",
        "tie_order_oracle_accessions",
        "tie_order_oracle_strict",
        "tie_order_oracle_text",
        "ungapped",
        "use_db_order_oracle",
        "use_local_ssd",
        "window_size",
        "word_size",
        "xdrop_gap",
        "xdrop_gap_final",
        "xdrop_ungap",
    }
)

_SEARCHSP_OPTION_RE = re.compile(r"(?<!\S)-searchsp(?:\s|=|$)")
_SUBMISSION_SOURCES = frozenset({"dashboard", "external_api", "servicebus"})

# Resource profiles the sibling OpenAPI plane treats as "shard this DB" (it
# splits the DB across nodes + applies the verified search-space correction).
# Mirrors the sibling's submit branch in docker-openapi/app/main.py.
_SHARDING_RESOURCE_PROFILES = frozenset({"core_nt_precise", "precise", "core_nt_safe"})

# Server-derived default profile for databases whose memory footprint exceeds a
# single node so they MUST run sharded. core_nt's bytes_to_cache is ~252 GB,
# which does not fit the 128 GB blast pool node — submitting it with the
# "standard" profile makes the sibling build a non-sharded config that
# elastic-blast rejects ("memory requirements exceed memory available"). The
# dashboard's own catalogue / API Reference already pairs core_nt with
# ``core_nt_safe``; this makes the Service Bus + direct OpenAPI submit paths
# apply that same default when the caller omits a profile. Keyed by bare DB
# name (see ``extract_db_name``). Only the sibling can shard, and it only
# shards core_nt today, so a static map is sufficient and accurate; generalise
# this in lockstep if the sibling gains sharding for more databases.
_SHARDED_DB_DEFAULT_PROFILE: dict[str, str] = {"core_nt": "core_nt_safe"}


@dataclass(frozen=True)
class PrecisionPlan:
    """Resolved sharding/search-space plan shared by every submit surface."""

    options: dict[str, Any]
    precision: dict[str, Any]
    compatibility_contract: dict[str, Any]
    validation_errors: list[str] = field(default_factory=list)
    downgraded: bool = False
    downgrade_reason: str | None = None


def resolve_sharded_db_resource_profile(
    database: str, requested_profile: Any
) -> str:
    """Promote a missing/standard profile to a DB's sharding default.

    Returns the resource profile the submit should carry. An explicit
    sharding-family profile (or any non-``standard`` caller value) is preserved;
    only an empty / ``standard`` profile is upgraded to the DB's sharding
    default. Unknown databases are returned unchanged (``standard`` default).
    Pure + side-effect-free so both submit paths can call it.
    """
    from api.services.blast.db_metadata import extract_db_name

    requested = str(requested_profile or "").strip()
    if requested in _SHARDING_RESOURCE_PROFILES:
        return requested
    db_name = extract_db_name(database) or str(database or "").strip()
    default = _SHARDED_DB_DEFAULT_PROFILE.get(db_name)
    if default and requested in ("", "standard"):
        return default
    return requested or "standard"


def canonical_submit_metadata(
    body: dict[str, Any],
    *,
    submission_source: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    if submission_source not in _SUBMISSION_SOURCES:
        raise ValueError("submission_source must be server-derived")

    metadata: dict[str, Any] = {
        "submission_source": submission_source,
        "external_correlation_id": correlation_id or str(uuid.uuid4()),
        "priority": 50,
        "resource_profile": "standard",
    }
    priority = body.get("priority")
    if isinstance(priority, int) and 0 <= priority <= 100:
        metadata["priority"] = priority
    resource_profile = body.get("resource_profile")
    if isinstance(resource_profile, str) and re.fullmatch(
        r"[A-Za-z0-9._-]{1,64}", resource_profile
    ):
        metadata["resource_profile"] = resource_profile
    idempotency_key = body.get("idempotency_key")
    if isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256:
        metadata["idempotency_key"] = idempotency_key
    return metadata


def canonical_submit_snapshot(body: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, side-effect-free submit snapshot for UI and OpenAPI payloads."""
    database = str(body.get("database") or body.get("db") or "").strip()
    program = str(body.get("program") or "blastn").strip()
    options = _canonical_options_from_body(body, database=database)
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "program": program,
        "database": database,
        "query": _canonical_query_from_body(body),
        "options": options,
        "metadata": {
            key: body[key]
            for key in (
                "submission_source",
                "external_correlation_id",
                "idempotency_key",
                "priority",
                "resource_profile",
            )
            if body.get(key) not in (None, "")
        },
    }
    for key in ("resource_group", "cluster_name", "aks_cluster_name", "storage_account"):
        if body.get(key) not in (None, ""):
            snapshot[key] = body[key]
    return snapshot


def canonical_execution_config(body: dict[str, Any]) -> dict[str, Any]:
    """Return the comparable execution core shared by dashboard and OpenAPI submits."""
    snapshot = canonical_submit_snapshot(body)
    return {
        "program": snapshot["program"],
        "database": snapshot["database"],
        "options": snapshot["options"],
        "query": {
            key: value
            for key, value in snapshot["query"].items()
            if key in {"kind", "query_count", "total_letters"}
        },
    }


def submit_contracts(body: dict[str, Any]) -> dict[str, Any]:
    """Return shared precision and compatibility contracts for a submit payload."""
    plan = resolve_sharding_plan(
        program=str(body.get("program") or "blastn").strip(),
        database=str(body.get("database") or body.get("db") or "").strip(),
        options=_canonical_options_from_body(
            body,
            database=str(body.get("database") or body.get("db") or "").strip(),
        ),
        caller_supplied_searchsp=_caller_supplied_searchsp(body),
    )
    precision = dict(plan.precision)
    compatibility = dict(plan.compatibility_contract)
    if plan.validation_errors:
        compatibility["eligible"] = False
        compatibility["level"] = "blocked"
        compatibility["blocking_errors"] = [
            *compatibility.get("blocking_errors", []),
            *plan.validation_errors,
        ]
    if plan.downgraded:
        compatibility["warnings"] = [
            *compatibility.get("warnings", []),
            plan.downgrade_reason or "sharding request was downgraded",
        ]
    return {
        "precision": precision,
        "compatibility_contract": compatibility,
    }


def resolve_sharding_plan(
    *,
    program: str,
    database: str,
    options: dict[str, Any] | None,
    caller_supplied_searchsp: int | None,
    allow_calibration_downgrade: bool = True,
) -> PrecisionPlan:
    """Resolve one canonical sharding/search-space plan for every submit surface."""
    from api.services.blast.compatibility import build_compatibility_contract
    from api.services.sharding_precision import (
        build_precision_report,
        merge_format_for_outfmt,
        normalize_sharding_mode,
        option_value,
        positive_int,
    )
    from api.services.web_blast_searchsp import default_for_database

    # Reserved for future program-specific calibration rules; keep the shared
    # interface stable across all submit surfaces even though today's resolution
    # is database/option driven.
    _ = program
    resolved = dict(options or {})
    validated_errors: list[str] = []
    downgrade_reason: str | None = None
    downgraded = False
    requested_mode = normalize_sharding_mode(resolved)
    verified_default = default_for_database(database)
    additional_searchsp = positive_int(
        option_value(str(resolved.get("additional_options") or ""), "-searchsp")
    )
    explicit_searchsp = caller_supplied_searchsp or positive_int(
        resolved.get("db_effective_search_space")
    ) or additional_searchsp
    snapshot_error = _calibration_snapshot_error(verified_default, resolved)

    if verified_default is not None:
        if explicit_searchsp is None:
            if (
                resolved.get("query_effective_search_spaces") in (None, "")
                and not _SEARCHSP_OPTION_RE.search(str(resolved.get("additional_options") or ""))
            ):
                resolved["db_effective_search_space"] = verified_default.value
        else:
            if snapshot_error is not None:
                validated_errors.append(snapshot_error)
            elif explicit_searchsp != verified_default.value:
                validated_errors.append(
                    "caller-supplied db_effective_search_space does not match the "
                    "calibrated Web BLAST search space"
                )
            else:
                resolved["db_effective_search_space"] = explicit_searchsp
    elif explicit_searchsp is not None:
        validated_errors.append(
            "caller-supplied db_effective_search_space requires a calibrated database snapshot"
        )

    if validated_errors and allow_calibration_downgrade:
        # The caller's verified Web BLAST search space does not apply to the
        # live database snapshot (the DB drifted from the calibration, or the
        # supplied value/snapshot does not match). Rather than hard-blocking the
        # submit, drop the calibrated search space so BLAST computes its own,
        # fall back from precise sharding (which requires the calibrated value)
        # to approximate when the output format can be merged across shards, and
        # surface a warning. Every submit surface (browser New Search and the
        # Service-Bus bridge alike) degrades identically.
        resolved.pop("db_effective_search_space", None)
        can_downgrade = True
        if requested_mode == "precise":
            merge_family = merge_format_for_outfmt(
                option_value(str(resolved.get("additional_options") or ""), "-outfmt")
                if resolved.get("additional_options")
                else resolved.get("outfmt")
            )
            if merge_family is not None:
                resolved["sharding_mode"] = "approximate"
            else:
                # The output format cannot be merged across shards, so
                # approximate sharding is unavailable — keep the original
                # blocking error rather than silently producing a bad run.
                can_downgrade = False
        if can_downgrade:
            resolved["sharding_mode"] = resolved.get("sharding_mode", requested_mode)
            downgraded = True
            downgrade_reason = (
                "verified Web BLAST search-space calibration does not match this "
                "database snapshot; using BLAST's own effective search space for "
                "this run (Web BLAST e-value parity not applied)"
            )
            validated_errors = []

    query_count = positive_int(resolved.get("query_count"))
    report = build_precision_report(
        resolved,
        query_count=query_count if isinstance(query_count, int) else resolved.get("query_count"),
        db_stats_available=bool(resolved.get("db_total_letters")),
        shard_sets=resolved.get("shard_sets")
        if isinstance(resolved.get("shard_sets"), list)
        else None,
    )
    compatibility = build_compatibility_contract(
        database=database,
        options=resolved,
        precision_report=report,
    )
    return PrecisionPlan(
        options={key: resolved[key] for key in sorted(resolved)},
        precision=report.as_dict(),
        compatibility_contract=compatibility.as_dict(),
        validation_errors=validated_errors,
        downgraded=downgraded,
        downgrade_reason=downgrade_reason,
    )


def _canonical_options_from_body(body: dict[str, Any], *, database: str) -> dict[str, Any]:
    options = _submit_options_from_body(body)
    query = _canonical_query_from_body(body)
    if (
        options.get("query_count") in (None, "")
        and isinstance(query.get("query_count"), int)
        and int(query["query_count"]) > 0
    ):
        options["query_count"] = int(query["query_count"])
    if "dust" in options and "low_complexity_filter" not in options:
        options["low_complexity_filter"] = bool(options["dust"])
    options.pop("dust", None)
    plan = resolve_sharding_plan(
        program=str(body.get("program") or "blastn").strip(),
        database=database,
        options=options,
        caller_supplied_searchsp=_caller_supplied_searchsp(body),
    )
    return plan.options


def _canonical_query_from_body(body: dict[str, Any]) -> dict[str, Any]:
    query_fasta = body.get("query_fasta") or body.get("query_data")
    if isinstance(query_fasta, str) and query_fasta.strip():
        query: dict[str, Any] = {
            "kind": "inline_fasta",
            "sha256": sha256(query_fasta.encode("utf-8")).hexdigest(),
        }
        try:
            from api.services.query_metadata import parse_fasta_metadata

            metadata = parse_fasta_metadata(query_fasta).as_dict()
            query.update(
                {
                    "query_count": metadata.get("query_count"),
                    "total_letters": metadata.get("total_letters"),
                    "records": metadata.get("records"),
                }
            )
        except Exception:
            query["metadata_status"] = "invalid"
        return query
    query_file = body.get("query_file") or body.get("query_blob_url")
    if query_file not in (None, ""):
        return {"kind": "query_file", "path": str(query_file)}
    accession = body.get("query_accession")
    if isinstance(accession, str) and accession.strip():
        # An NCBI nuccore accession resolves to exactly one FASTA record (a
        # subrange narrows that record but does not change the count), so the
        # query count is deterministically 1. The accession is only fetched
        # later in ``_normalise_blast_submit_body``; declaring the count here
        # lets the pre-side-effect precision contract validate precise
        # sharding instead of failing with "precise sharding requires query
        # metadata" before the fetch happens.
        return {
            "kind": "ncbi_accession",
            "accession": accession.strip(),
            "query_count": 1,
        }
    return {"kind": "missing"}


def _submit_options_from_body(body: dict[str, Any]) -> dict[str, Any]:
    raw_options = body.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    raw_searchsp = options.pop("searchsp", None)
    if raw_searchsp not in (None, "") and "db_effective_search_space" not in options:
        options["db_effective_search_space"] = raw_searchsp
    for key in _BLAST_SUBMIT_OPTION_KEYS:
        if key in body and body[key] not in (None, ""):
            options.setdefault(key, body[key])
    if "searchsp" in body and body["searchsp"] not in (None, ""):
        options.setdefault("db_effective_search_space", body["searchsp"])
    options["use_local_ssd"] = True
    return options


def _apply_web_blast_searchsp_default(database: str, options: dict[str, Any]) -> None:
    from api.services.web_blast_searchsp import default_for_database

    default = default_for_database(database)
    if default is None:
        return

    if options.get("db_effective_search_space") not in (None, ""):
        pass
    elif options.get("query_effective_search_spaces") not in (None, ""):
        pass
    elif not _SEARCHSP_OPTION_RE.search(str(options.get("additional_options") or "")):
        options["db_effective_search_space"] = default.value

    options.setdefault("low_complexity_filter", True)


def _caller_supplied_searchsp(body: dict[str, Any]) -> int | None:
    raw_options = body.get("options")
    if isinstance(raw_options, dict):
        candidate = raw_options.get("db_effective_search_space")
        if candidate not in (None, ""):
            return _positive_int(candidate)
        candidate = raw_options.get("searchsp")
        if candidate not in (None, ""):
            return _positive_int(candidate)
    for key in ("db_effective_search_space", "searchsp"):
        candidate = body.get(key)
        if candidate not in (None, ""):
            return _positive_int(candidate)
    return None


def _positive_int(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _calibration_snapshot_error(
    verified_default: Any | None,
    options: dict[str, Any],
) -> str | None:
    if verified_default is None:
        return None
    observed_db_len = _positive_int(options.get("db_total_letters"))
    calibrated_db_len = _positive_int(getattr(verified_default, "calibrated_db_len", None))
    if (
        observed_db_len is not None
        and calibrated_db_len is not None
        and observed_db_len != calibrated_db_len
    ):
        return (
            "caller-supplied db_effective_search_space does not match the calibrated "
            "database snapshot"
        )
    observed_db_num = _positive_int(
        options.get("db_total_sequences") or options.get("db_num") or options.get("db_entries")
    )
    calibrated_db_num = _positive_int(getattr(verified_default, "calibrated_db_num", None))
    if (
        observed_db_num is not None
        and calibrated_db_num is not None
        and observed_db_num != calibrated_db_num
    ):
        return (
            "caller-supplied db_effective_search_space does not match the calibrated "
            "database snapshot"
        )
    return None


def _upload_inline_query_for_submit(
    *,
    job_id: str,
    storage_account: str,
    query_data: str,
) -> tuple[str, dict[str, object]]:
    from api.services import get_credential
    from api.services.query_metadata import parse_fasta_metadata
    from api.services.storage.data import upload_query_text

    query_metadata = parse_fasta_metadata(query_data)
    blob_path = f"uploads/{job_id}/query.fa"
    try:
        upload_query_text(
            get_credential(),
            storage_account,
            "queries",
            blob_path,
            query_data,
        )
    except Exception as exc:
        raise HTTPException(
            503,
            detail={
                "code": "query_upload_failed",
                "message": f"Could not upload inline query FASTA: {type(exc).__name__}",
                "retryable": True,
            },
        ) from exc
    return f"queries/{blob_path}", query_metadata.as_dict()


def _normalise_blast_submit_body(body: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    normalised = dict(body)
    normalised.update(
        canonical_submit_metadata(
            body,
            submission_source="dashboard",
            correlation_id=job_id,
        )
    )
    if not normalised.get("cluster_name") and normalised.get("aks_cluster_name"):
        normalised["cluster_name"] = normalised["aks_cluster_name"]
    if not normalised.get("database") and normalised.get("db"):
        normalised["database"] = normalised["db"]
    if not normalised.get("query_file") and normalised.get("query_blob_url"):
        normalised["query_file"] = normalised["query_blob_url"]

    options = _submit_options_from_body(normalised)
    _apply_web_blast_searchsp_default(
        str(normalised.get("database") or normalised.get("db") or ""),
        options,
    )
    normalised["use_local_ssd"] = True

    # Accession-sourced query: resolve to FASTA up front so the existing
    # ``query_data`` upload path handles staging unchanged. Skipped when the
    # caller already supplied inline FASTA or a query_file pointer.
    raw_accession = normalised.get("query_accession")
    if isinstance(raw_accession, str) and raw_accession.strip():
        # Reject mixed query sources explicitly so OpenAPI / dashboard /
        # external callers all see the same error instead of silently
        # dropping one input. Per-call precedence makes audit + replay
        # ambiguous, so we treat this as a validation conflict.
        if (
            normalised.get("query_data")
            or normalised.get("query_file")
            or normalised.get("query_blob_url")
        ):
            raise HTTPException(
                422,
                detail={
                    "code": "conflicting_query_sources",
                    "message": (
                        "Specify either query_accession or one of "
                        "query_data / query_file / query_blob_url, not both."
                    ),
                },
            )
        from api.services.blast.accession_resolver import resolve_accession_to_fasta

        fasta_text, accession_metadata = resolve_accession_to_fasta(
            raw_accession.strip(),
            seq_start=normalised.get("query_accession_seq_start"),
            seq_stop=normalised.get("query_accession_seq_stop"),
        )
        normalised["query_data"] = fasta_text
        # Strip the accession-only fields so they do not leak into the
        # downstream Pydantic model or the elastic-blast config.
        for key in (
            "query_accession",
            "query_accession_seq_start",
            "query_accession_seq_stop",
        ):
            normalised.pop(key, None)
        normalised["_accession_metadata"] = accession_metadata

    query_data = normalised.get("query_data")
    if isinstance(query_data, str) and query_data.strip():
        try:
            from api.services.query_metadata import parse_fasta_metadata

            query_metadata = parse_fasta_metadata(query_data).as_dict()
        except Exception as exc:
            raise HTTPException(
                422,
                detail={"code": "invalid_query_fasta", "message": str(exc)[:500]},
            ) from exc
        options.setdefault("query_count", query_metadata.get("query_count"))
        if not normalised.get("query_file"):
            storage_account = str(normalised.get("storage_account") or "")
            if not storage_account:
                raise HTTPException(
                    422,
                    detail={
                        "code": "validation_error",
                        "message": "storage_account is required when query_data is submitted",
                    },
                )
            query_file, query_metadata = _upload_inline_query_for_submit(
                job_id=job_id,
                storage_account=storage_account,
                query_data=query_data,
            )
            normalised["query_file"] = query_file
        normalised["query_metadata"] = query_metadata
        normalised.pop("query_data", None)

    # Merge accession provenance into query_metadata after the upload path has
    # filled in length/count fields. We do this last so a manual fasta upload
    # cannot accidentally inherit accession metadata from a prior call.
    accession_metadata = normalised.pop("_accession_metadata", None)
    if isinstance(accession_metadata, dict):
        merged = dict(normalised.get("query_metadata") or {})
        merged.update(accession_metadata)
        normalised["query_metadata"] = merged

    if options:
        normalised["options"] = options
    normalised["canonical_request"] = canonical_submit_snapshot(normalised)
    return normalised
