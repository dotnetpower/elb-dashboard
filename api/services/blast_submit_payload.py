"""BLAST submit payload normalization helpers."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

_BLAST_SUBMIT_OPTION_KEYS = frozenset(
    {
        "additional_options",
        "acr_name",
        "acr_resource_group",
        "allow_approximate_sharding",
        "batch_len",
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
        "is_inclusive",
        "low_complexity_filter",
        "machine_type",
        "max_target_seqs",
        "mem_limit",
        "mem_request",
        "num_nodes",
        "outfmt",
        "pd_size",
        "query_count",
        "query_effective_search_spaces",
        "reuse",
        "shard_sets",
        "sharding_mode",
        "taxid",
        "tie_order_oracle_accessions",
        "tie_order_oracle_strict",
        "tie_order_oracle_text",
        "use_db_order_oracle",
        "use_local_ssd",
        "word_size",
    }
)

_SEARCHSP_OPTION_RE = re.compile(r"(?<!\S)-searchsp(?:\s|=|$)")


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


def _upload_inline_query_for_submit(
    *,
    job_id: str,
    storage_account: str,
    query_data: str,
) -> tuple[str, dict[str, object]]:
    from api.services import get_credential
    from api.services.query_metadata import parse_fasta_metadata
    from api.services.storage_data import upload_query_text

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

    if options:
        normalised["options"] = options
    return normalised
