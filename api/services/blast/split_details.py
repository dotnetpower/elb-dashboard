"""Project split-query child job rows into a bounded shard-detail response.

Responsibility: Convert existing child JobState rows into sanitized, read-only shard progress
    details and aggregate counts for the Results UI.
Edit boundaries: Pure projection only; repository access and caller authorization belong to
    routes, while split execution and state transitions remain in tasks.
Key entry points: ``build_split_details``, ``SplitDetailsResponse``.
Risky contracts: Never expose child payloads, Storage URLs, subscription ids, or unsanitized
    task errors. Child ordering must be deterministic across repeated reads.
Validation: ``uv run pytest -q api/tests/test_blast_shard_details.py``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from api.services.sanitise import sanitise

_COMPLETED = frozenset({"completed", "succeeded", "success"})
_FAILED = frozenset({"failed", "error"})
_CANCELLED = frozenset({"cancelled", "canceled", "deleted"})
_ACTIVE = frozenset({"queued", "pending", "submitted", "running", "reducing"})
_MAX_EFFECTIVE_SEARCH_SPACE = (1 << 127) - 1


class ShardDetail(BaseModel):
    """Safe status projection for one split child job."""

    job_id: str
    group_id: str | None = None
    query_file: str | None = None
    status: str
    phase: str
    created_at: str | None = None
    updated_at: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    effective_search_space: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error: str | None = None


class SplitDetailsResponse(BaseModel):
    """Aggregate and per-child details for one split-query parent."""

    schema_version: int = 1
    parent_job_id: str
    truncated: bool = False
    summary: dict[str, int | float]
    shards: list[ShardDetail]


def _duration_seconds(created_at: object, updated_at: object) -> float | None:
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        return None
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    try:
        return max(0.0, round((end - start).total_seconds(), 3))
    except TypeError:
        return None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_text(value: object, max_length: int) -> str | None:
    text = _optional_text(value)
    return sanitise(text)[:max_length] if text else None


def _query_filename(value: object) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    path = text.split("?", 1)[0].split("#", 1)[0]
    return sanitise(PurePosixPath(path.replace("\\", "/")).name)[:160] or None


def build_split_details(
    parent_job_id: str,
    children: list[Any],
    *,
    truncated: bool = False,
) -> SplitDetailsResponse:
    """Return deterministic shard details from existing child rows."""
    shards: list[ShardDetail] = []
    counts = {"completed": 0, "failed": 0, "cancelled": 0, "active": 0, "other": 0}
    for child in children:
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        status = (_safe_text(getattr(child, "status", None), 64) or "unknown").casefold()
        phase = _safe_text(getattr(child, "phase", None), 80) or status
        if status in _COMPLETED:
            counts["completed"] += 1
        elif status in _FAILED:
            counts["failed"] += 1
        elif status in _CANCELLED:
            counts["cancelled"] += 1
        elif status in _ACTIVE:
            counts["active"] += 1
        else:
            counts["other"] += 1

        raw_search_space = payload.get("effective_search_space")
        effective_search_space: int | None = None
        if isinstance(raw_search_space, (str, int, float)) and not isinstance(
            raw_search_space, bool
        ):
            try:
                parsed_search_space = int(raw_search_space)
                if 0 <= parsed_search_space <= _MAX_EFFECTIVE_SEARCH_SPACE:
                    effective_search_space = parsed_search_space
            except (OverflowError, ValueError):
                pass
        raw_error = payload.get("error") or getattr(child, "error_code", None)
        error = sanitise(str(raw_error))[:300] if raw_error else None
        shards.append(
            ShardDetail(
                job_id=_safe_text(getattr(child, "job_id", None), 128) or "",
                group_id=_safe_text(payload.get("group_id"), 160),
                query_file=_query_filename(payload.get("query_file")),
                status=status,
                phase=phase,
                created_at=_safe_text(getattr(child, "created_at", None), 64),
                updated_at=_safe_text(getattr(child, "updated_at", None), 64),
                duration_seconds=_duration_seconds(
                    getattr(child, "created_at", None),
                    getattr(child, "updated_at", None),
                ),
                effective_search_space=effective_search_space,
                error_code=_safe_text(getattr(child, "error_code", None), 80),
                error=error,
            )
        )

    shards.sort(key=lambda shard: (shard.group_id or "", shard.created_at or "", shard.job_id))
    total = len(shards)
    terminal = counts["completed"] + counts["failed"] + counts["cancelled"]
    return SplitDetailsResponse(
        parent_job_id=parent_job_id,
        truncated=truncated,
        summary={
            "total": total,
            **counts,
            "terminal": terminal,
            "progress_percent": round((terminal / total) * 100, 1) if total else 0.0,
        },
        shards=shards,
    )
