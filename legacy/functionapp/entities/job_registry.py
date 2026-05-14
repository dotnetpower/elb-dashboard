"""Durable Entity for tracking BLAST job registry.

Stores a list of job summaries so the UI can list all submitted jobs
without scanning blob storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import azure.durable_functions as df


def job_registry_entity(context: df.DurableEntityContext) -> None:
    """Entity operations: add_job, update_job, list_jobs, get_job, remove_job."""
    state: list[dict[str, Any]] = context.get_state(lambda: [])
    op = context.operation_name

    if op == "add_job":
        job = context.get_input()
        if not any(j.get("job_id") == job.get("job_id") for j in state):
            job["created_at"] = datetime.now(UTC).isoformat()
            job["updated_at"] = job["created_at"]
            state.append(job)
        context.set_state(state)

    elif op == "update_job":
        update = context.get_input()
        job_id = update.get("job_id")
        for i, j in enumerate(state):
            if j.get("job_id") == job_id:
                state[i] = {**j, **update, "updated_at": datetime.now(UTC).isoformat()}
                break
        context.set_state(state)

    elif op == "list_jobs":
        context.set_result(state)

    elif op == "get_job":
        job_id = context.get_input()
        found = next((j for j in state if j.get("job_id") == job_id), None)
        context.set_result(found)

    elif op == "remove_job":
        job_id = context.get_input()
        state = [j for j in state if j.get("job_id") != job_id]
        context.set_state(state)
