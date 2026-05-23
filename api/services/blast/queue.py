"""Queue visibility helpers for BLAST jobs.

Responsibility: Queue visibility helpers for BLAST jobs
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `queue_snapshot`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

from typing import Any

_ACTIVE_STATUSES = frozenset({"queued", "pending", "running", "reducing"})
_QUEUED_STATUSES = frozenset({"queued", "pending"})


def queue_snapshot(rows: list[Any], *, job_id: str | None = None) -> dict[str, Any]:
    active = [row for row in rows if str(getattr(row, "status", "")) in _ACTIVE_STATUSES]
    active.sort(key=lambda row: str(getattr(row, "created_at", "")))
    queued = [row for row in active if str(getattr(row, "status", "")) in _QUEUED_STATUSES]
    running = [row for row in active if str(getattr(row, "status", "")) not in _QUEUED_STATUSES]
    position = None
    if job_id:
        for index, row in enumerate(queued, start=1):
            if str(getattr(row, "job_id", "")) == job_id:
                position = index
                break
    return {
        "active_count": len(active),
        "queued_count": len(queued),
        "running_count": len(running),
        "job_id": job_id,
        "queue_position": position,
    }
