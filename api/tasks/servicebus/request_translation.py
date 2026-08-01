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
    profile; caller search-space flags are never overwritten; v1 calibration
    failure degrades without rejecting an otherwise valid request.
Validation: ``uv run pytest -q api/tests/test_servicebus_tasks.py
    api/tests/test_servicebus_v1_multitoken.py
    api/tests/test_blast_submit_route_options.py``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

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
    from api.services.blast.submit_payload import (
        _caller_supplied_searchsp,
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
        "evalue",
        "max_target_seqs",
        "sharding_mode",
        "db_effective_search_space",
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
    ):
        if body.get(key) is not None:
            candidate[key] = body[key]

    try:
        request = ExternalBlastSubmitRequest(**candidate)
    except Exception:
        logger.warning("service bus request validation failed corr=%s", correlation_id)
        return None

    payload = request.model_dump(exclude_none=True)
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "",
        payload.get("resource_profile"),
    )
    plan = resolve_sharding_plan(
        program=str(payload.get("program") or "blastn"),
        database=str(payload.get("db") or ""),
        options=payload.get("options"),
        caller_supplied_searchsp=_caller_supplied_searchsp(body),
        allow_servicebus_downgrade=True,
    )
    payload["options"] = plan.options
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="servicebus",
            correlation_id=correlation_id,
        )
    )
    return cast(dict[str, Any], payload)


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
    for key in ("taxid", "is_inclusive", "priority", "batch_len", "idempotency_key"):
        if body.get(key) is not None:
            candidate[key] = body[key]

    try:
        request = ExternalBlastV1Request(**candidate)
    except Exception:
        logger.warning("service bus v1 request validation failed corr=%s", correlation_id)
        return None

    payload = request.model_dump(exclude_none=True)
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "",
        payload.get("resource_profile"),
    )
    blast_options = payload.get("blast_options")
    if isinstance(blast_options, dict):
        caller_searchsp = blast_options.pop("db_effective_search_space", None)
        existing = f"{blast_options.get('extra') or ''} {blast_options.get('outfmt') or ''}"
        if "-searchsp" not in existing and "-dbsize" not in existing:
            resolved_searchsp = None
            plan = None
            try:
                plan = resolve_sharding_plan(
                    program=str(payload.get("program") or "blastn"),
                    database=str(payload.get("db") or ""),
                    options={
                        "additional_options": str(blast_options.get("extra") or ""),
                        "db_effective_search_space": caller_searchsp,
                        "db_total_letters": body.get("db_total_letters"),
                        "db_total_sequences": body.get("db_total_sequences"),
                    },
                    caller_supplied_searchsp=(
                        caller_searchsp if isinstance(caller_searchsp, int) else None
                    ),
                    allow_servicebus_downgrade=True,
                )
                resolved_searchsp = plan.options.get("db_effective_search_space")
            except Exception as exc:
                logger.warning(
                    "service bus v1 searchsp resolution skipped corr=%s: %s",
                    correlation_id,
                    type(exc).__name__,
                )
            if resolved_searchsp:
                current_extra = str(blast_options.get("extra") or "").strip()
                blast_options["extra"] = (
                    f"{current_extra} -searchsp {int(resolved_searchsp)}".strip()
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
    return cast(dict[str, Any], payload)
