"""BLAST submit ingress routing — optional Service Bus enqueue front door.

Decides whether an inline-FASTA BLAST submit goes straight to the OpenAPI
execution plane (the historical direct path) or is enqueued onto the Service Bus
request queue so the dashboard's own consumer drains it (the unified-ingress
path from issue #36). The switch is a default-OFF env gate so flipping the live
submit contract is an explicit, reversible operator action (charter §12a Rule 4).

Responsibility: One decision — ``should_enqueue_submit()`` — plus the enqueue
    action ``enqueue_submit_request()`` that publishes a request message and
    records the ``enqueued`` trace stage. NO direct OpenAPI submit here (the
    route owns the fallback), NO drain logic (that is the consumer task).
Edit boundaries: Keep this module free of FastAPI types and of the OpenAPI
    client. Service Bus access goes through ``api.services.service_bus``.
Key entry points: ``should_enqueue_submit``, ``enqueue_submit_request``,
    ``SB_SUBMIT_INGRESS_ENV``.
Risky contracts: ``should_enqueue_submit`` returns True ONLY when the gate env
    is truthy AND the Service Bus integration is actually enabled — so a
    half-configured deployment never silently drops submits into a void.
    ``enqueue_submit_request`` raises on a real publish failure so the caller can
    fall back to the direct path (it must NOT swallow — a swallowed failure would
    lose the job).
Validation: ``uv run pytest -q api/tests/test_submit_ingress.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

LOGGER = logging.getLogger(__name__)

# Default-OFF gate. When unset/false the historical direct `/v1/jobs` submit path
# is used unchanged. When true (AND Service Bus is enabled) the dashboard API
# becomes a producer: it enqueues the request and its own consumer drains it,
# so every submit shares the single consumer = single writer path.
SB_SUBMIT_INGRESS_ENV = "ENABLE_SB_SUBMIT_INGRESS"


def should_enqueue_submit() -> bool:
    """True when an inline-FASTA submit should be enqueued to Service Bus.

    Requires BOTH the explicit gate env AND a live Service Bus integration. If
    the gate is on but Service Bus is not actually enabled we return False so the
    route keeps using the direct path instead of dropping the submit.
    """
    if os.environ.get(SB_SUBMIT_INGRESS_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return False
    try:
        from api.services.service_bus_pref import service_bus_enabled

        return bool(service_bus_enabled())
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("submit ingress gate check failed: %s", type(exc).__name__)
        return False


def enqueue_submit_request(payload: dict[str, Any], correlation_id: str) -> str:
    """Publish a BLAST request message onto the Service Bus request queue.

    Returns the message_id. Raises on a publish failure so the caller can fall
    back to the direct submit path (a swallowed failure would silently lose the
    job). Records the ``enqueued`` trace stage best-effort so the message
    lifecycle starts on the dashboard the instant it is enqueued — even before
    the consumer drains it.

    The message body mirrors the OpenAPI ``/v1/jobs`` request shape the consumer
    already parses (``_build_request_payload``): inline ``query_fasta`` + ``db``
    + ``program`` + ``options`` + the optional taxonomy / batch / idempotency
    fields, all of which the submit payload already carries.
    """
    from api.services import service_bus
    from api.services.service_bus_pref import get_service_bus_config

    cfg = get_service_bus_config()
    body: dict[str, Any] = {"external_correlation_id": correlation_id}
    for key in (
        "query_fasta",
        "db",
        "program",
        "options",
        "taxid",
        "is_inclusive",
        "priority",
        "batch_len",
        "idempotency_key",
        "resource_profile",
    ):
        value = payload.get(key)
        if value is not None:
            body[key] = value

    from api.services.blast.request_subject import build_request_subject

    message_id = service_bus.send_request(
        cfg, body, correlation_id=correlation_id, subject=build_request_subject(body)
    )

    # The job has no OpenAPI id yet (the consumer assigns it at drain time), so
    # the lifecycle trace is keyed by the dashboard correlation id here. The
    # consumer re-keys subsequent stages by the OpenAPI id; the two are linked
    # via the bridge record + the row's external_correlation_id column.
    try:
        from api.services.blast.message_trace import record_stage
        from api.services.state_repo import get_state_repo

        record_stage(
            get_state_repo(),
            correlation_id,
            "enqueued",
            message_id=message_id,
            via="dashboard_api",
        )
    except Exception as exc:  # pragma: no cover - best-effort
        LOGGER.debug("submit ingress enqueue trace skipped: %s", type(exc).__name__)

    return message_id
