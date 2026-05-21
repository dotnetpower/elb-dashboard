"""Persist ElasticBLAST ``submit`` log lines as artifact chunks.

Responsibility: Slice the in-memory ``submit`` log event list into fixed-size
chunks and hand each one to :mod:`api.services.job_artifacts` so the UI can
stream the historical log without buffering the whole submit window in
memory.
Edit boundaries: Persistence wrapper only — no parsing, no Redis, no Celery.
Live progress updates and lock management belong in the orchestrating task.
Key entry points: ``SUBMIT_LOG_CHUNK_EVENT_COUNT``,
``persist_submit_log_events``.
Risky contracts: Persistence failures must never propagate; the live submit
must succeed even if artifact storage is unavailable. We log a single debug
line and move on.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

SUBMIT_LOG_CHUNK_EVENT_COUNT = 100


def persist_submit_log_events(
    *,
    job_id: str,
    progress_phase: str,
    events: list[dict[str, Any]],
) -> None:
    if not events:
        return
    try:
        from api.services.job_artifacts import write_execution_log_chunk

        for chunk_sequence, start in enumerate(
            range(0, len(events), SUBMIT_LOG_CHUNK_EVENT_COUNT)
        ):
            write_execution_log_chunk(
                job_id,
                progress_phase,
                chunk_sequence,
                events[start : start + SUBMIT_LOG_CHUNK_EVENT_COUNT],
            )
    except Exception as exc:
        LOGGER.debug(
            "submit log chunk persistence skipped job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
