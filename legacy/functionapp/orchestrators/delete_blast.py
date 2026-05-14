"""BLAST job delete orchestrator.

Sequence:
  1. Run elastic-blast delete on Remote Terminal VM
  2. Signal entity with final status

Output: dict with job_id and status.
"""

from __future__ import annotations

import logging
from typing import Any

import azure.durable_functions as df

LOGGER = logging.getLogger(__name__)


def delete_blast_orchestrator(
    context: df.DurableOrchestrationContext,
) -> dict[str, Any]:
    request: dict[str, Any] = context.get_input() or {}
    job_id = request.get("job_id", "")

    context.set_custom_status({"phase": "deleting", "job_id": job_id})

    try:
        result = yield context.call_activity("run_elastic_blast_delete_activity", request)
        success = result.get("success", False)
    except Exception as exc:
        LOGGER.warning("elastic-blast delete failed for job=%s: %s", job_id, exc)
        success = False

    final_status = "deleted" if success else "delete_failed"

    # Signal entity with final status
    entity_id = df.EntityId("job_registry_entity", "default")
    context.signal_entity(
        entity_id,
        "update_job",
        {"job_id": job_id, "status": final_status, "phase": final_status},
    )

    return {"job_id": job_id, "status": final_status}
