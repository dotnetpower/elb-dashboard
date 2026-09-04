"""Translate Service Bus request messages into sibling OpenAPI payloads.

Responsibility: Validate XML and free-form queue request bodies and derive the
    server-owned correlation, sharding profile, and search-space metadata sent
    to the sibling OpenAPI execution plane.
Edit boundaries: Request validation and payload shaping only. Queue settlement,
    admission, OpenAPI calls, persistence, and producer responses remain in
    ``api.tasks.servicebus.tasks``.
Key entry points: ``build_request_payload``, ``is_v1_jobs_message``,
    ``build_v1_jobs_payload``.
Risky contracts: Correlation fallback order is body, Service Bus correlation,
    then message id; invalid requests return ``None`` for terminal rejection;
    producers cannot spoof submission source; core_nt retains its safe sharded
    profile; custom v1 profiles and target scope survive validation; caller
    search-space flags are preserved only without a configured workload account;
    deployed precise core_nt requests replace caller searchsp/dbsize from active
    metadata and fail closed when that metadata is unavailable.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py
    api/tests/test_servicebus_v1_multitoken.py
    api/tests/test_blast_submit_route_options.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from api.services.service_bus import ParsedMessage
from api.services.service_bus_pref import ServiceBusConfig


def build_request_payload(
    msg: ParsedMessage,
    cfg: ServiceBusConfig,
    *,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Map one XML-path queue message to a validated OpenAPI payload."""
    del cfg
    from api.routes.elastic_blast import ExternalBlastSubmitRequest
    from api.services.blast.live_search_space import (
        LiveSearchSpaceUnavailable,
        canonicalize_precise_options,
        collapse_uniform_query_search_space,
    )
    from api.services.blast.submit_payload import (
        align_options_with_resource_profile,
        canonical_submit_metadata,
        resolve_sharded_db_resource_profile,
        resolve_sharding_plan,
    )

    body = dict(msg.body or {})
    correlation_id = (
        str(body.get("external_correlation_id") or "").strip()
        or (msg.correlation_id or "").strip()
        or (msg.message_id or "").strip()
    )
    if not correlation_id:
        return None

    options: dict[str, Any] = {}
    raw_options = body.get("options")
    if isinstance(raw_options, dict):
        options.update(raw_options)
    for key in (
        "outfmt",
        "word_size",
        "dust",
        "soft_masking",
        "evalue",
        "max_target_seqs",
        "sharding_mode",
        "db_effective_search_space",
        "db_total_letters",
        "db_total_sequences",
    ):
        if key in body and key not in options:
            options[key] = body[key]

    candidate: dict[str, Any] = {
        "query_fasta": body.get("query_fasta"),
        "db": body.get("db"),
        "program": body.get("program") or "blastn",
        "external_correlation_id": correlation_id,
    }
    if options:
        candidate["options"] = options
    for key in (
        "taxid",
        "is_inclusive",
        "priority",
        "batch_len",
        "idempotency_key",
        "resource_profile",
        "subscription_id",
        "resource_group",
        "cluster_name",
    ):
        if body.get(key) is not None:
            candidate[key] = body[key]

    try:
        request = ExternalBlastSubmitRequest(**candidate)
    except Exception:
        logger.warning("service bus request validation failed corr=%s", correlation_id)
        return None

    payload: dict[str, Any] = request.model_dump(exclude_none=True)
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "",
        payload.get("resource_profile"),
    )
    payload["options"] = align_options_with_resource_profile(
        payload.get("options"), str(payload["resource_profile"])
    )
    try:
        payload["options"] = canonicalize_precise_options(
            str(payload.get("db") or ""),
            payload["options"],
        )
    except LiveSearchSpaceUnavailable:
        logger.warning(
            "service bus active search-space metadata unavailable corr=%s db=%s",
            correlation_id,
            payload.get("db"),
        )
        return None
    plan = resolve_sharding_plan(
        program=str(payload.get("program") or "blastn"),
        database=str(payload.get("db") or ""),
        options=payload["options"],
        caller_supplied_searchsp=None,
        allow_servicebus_downgrade=True,
    )
    payload["options"] = plan.options
    try:
        payload["options"] = collapse_uniform_query_search_space(payload["options"])
    except ValueError:
        logger.warning(
            "service bus query search-space transport unsupported corr=%s",
            correlation_id,
        )
        return None
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="servicebus",
            correlation_id=correlation_id,
        )
    )
    return payload


def is_v1_jobs_message(body: dict[str, Any]) -> bool:
    """Return whether a message selects the free-form ``/v1/jobs`` path."""
    return isinstance(body.get("blast_options"), dict)


def build_v1_jobs_payload(
    msg: ParsedMessage,
    cfg: ServiceBusConfig,
    *,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Map one free-form ``blast_options`` message to a v1 jobs payload."""
    del cfg
    from api.routes.elastic_blast import ExternalBlastV1Request
    from api.services.blast.live_search_space import (
        LiveSearchSpaceUnavailable,
        canonicalize_precise_options,
        set_search_space_option,
    )
    from api.services.blast.submit_payload import (
        canonical_submit_metadata,
        resolve_sharded_db_resource_profile,
        resolve_sharding_plan,
    )

    body = dict(msg.body or {})
    correlation_id = (
        str(body.get("external_correlation_id") or "").strip()
        or (msg.correlation_id or "").strip()
        or (msg.message_id or "").strip()
    )
    if not correlation_id:
        return None

    candidate: dict[str, Any] = {
        "query_fasta": body.get("query_fasta"),
        "db": body.get("db"),
        "program": body.get("program") or "blastn",
        "external_correlation_id": correlation_id,
    }
    if isinstance(body.get("blast_options"), dict):
        candidate["blast_options"] = body["blast_options"]
    for key in (
        "taxid",
        "is_inclusive",
        "priority",
        "batch_len",
        "idempotency_key",
        "resource_profile",
        "subscription_id",
        "resource_group",
        "cluster_name",
    ):
        if body.get(key) is not None:
            candidate[key] = body[key]

    try:
        request = ExternalBlastV1Request(**candidate)
    except Exception:
        logger.warning("service bus v1 request validation failed corr=%s", correlation_id)
        return None

    payload: dict[str, Any] = request.model_dump(exclude_none=True)
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "",
        payload.get("resource_profile"),
    )
    blast_options = payload.get("blast_options")
    if isinstance(blast_options, dict):
        caller_searchsp = blast_options.pop("db_effective_search_space", None)
        resolved_searchsp = None
        plan = None
        try:
            live_options = canonicalize_precise_options(
                str(payload.get("db") or ""),
                {
                    "sharding_mode": "precise",
                    "db_effective_search_space": caller_searchsp,
                    "db_total_letters": body.get("db_total_letters"),
                    "db_total_sequences": body.get("db_total_sequences"),
                    "additional_options": str(blast_options.get("extra") or ""),
                },
            )
            plan = resolve_sharding_plan(
                program=str(payload.get("program") or "blastn"),
                database=str(payload.get("db") or ""),
                options=live_options,
                caller_supplied_searchsp=None,
                allow_servicebus_downgrade=True,
            )
            resolved_searchsp = plan.options.get("db_effective_search_space")
        except LiveSearchSpaceUnavailable:
            logger.warning(
                "service bus v1 active search-space metadata unavailable corr=%s db=%s",
                correlation_id,
                payload.get("db"),
            )
            return None
        except Exception as exc:
            logger.warning(
                "service bus v1 searchsp resolution skipped corr=%s: %s",
                correlation_id,
                type(exc).__name__,
            )
        if resolved_searchsp:
            current_extra = str(blast_options.get("extra") or "").strip()
            blast_options["extra"] = set_search_space_option(
                current_extra,
                int(resolved_searchsp),
            )
            logger.info(
                "service bus v1 searchsp applied corr=%s db=%s searchsp=%s",
                correlation_id,
                payload.get("db"),
                int(resolved_searchsp),
            )
        elif plan is not None and getattr(plan, "downgraded", False):
            logger.info(
                "service bus v1 searchsp parity downgraded corr=%s db=%s reason=%s",
                correlation_id,
                payload.get("db"),
                getattr(plan, "downgrade_reason", None),
            )
        payload["blast_options"] = blast_options

    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="servicebus",
            correlation_id=correlation_id,
        )
    )
    payload["submission_source"] = "external_api"
    return payload
