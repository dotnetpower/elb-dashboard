"""BLAST task progress payload helpers.

Responsibility: BLAST task progress payload helpers
Edit boundaries: Keep long-running side effects here; route handlers should enqueue tasks and
persist state.
Key entry points: `_now_iso`, `_snippet`, `_step_key_for_phase`
Risky contracts: Tasks should be idempotent, retry-aware, and write progress/state checkpoints.
Validation: `uv run pytest -q api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

LIVE_OUTPUT_SNIPPET_CHARS = 8000
PROGRESS_STEP_ORDER = (
    "preparing",
    "warming_up",
    "configuring",
    "staging_db",
    "submitting",
    "running",
    "exporting_results",
    "completed",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _snippet(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _step_key_for_phase(phase: str) -> str:
    return {
        "queued": "preparing",
        "preparing": "preparing",
        "configuring": "configuring",
        "warmup_ready": "warming_up",
        "waiting_for_warmup": "warming_up",
        "warmup_not_ready": "warming_up",
        "warmup_failed": "warming_up",
        "staging_db": "staging_db",
        "submitting": "submitting",
        "submitted": "submitting",
        "submit_retryable_failure": "submitting",
        "submit_failed": "submitting",
        "split_children_submitted": "submitting",
        "split_children_aggregating": "running",
        "split_children_merge_ready": "exporting_results",
        "split_results_waiting_for_artifacts": "exporting_results",
        "split_results_merging": "exporting_results",
        "results_pending": "exporting_results",
        "completed": "completed",
        "failed": "submitting",
    }.get(phase, phase)


def _tail_text(lines: list[str], limit: int = LIVE_OUTPUT_SNIPPET_CHARS) -> str:
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    return text[-limit:]


def _compact_progress_details(details: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "config_blob_path",
        "config_url",
        "config_upload_error",
        "decision",
        "duration_ms",
        "exit_code",
        "k8s",
        "last_output",
        "log_line_count",
        "output",
        "timed_out",
    }
    compact: dict[str, Any] = {}
    for key, value in details.items():
        if key not in allowed:
            continue
        if key in {"last_output", "output"}:
            compact[key] = _snippet(value, LIVE_OUTPUT_SNIPPET_CHARS)
        else:
            compact[key] = value
    return compact


def _merge_progress_payload(
    existing_payload: Mapping[str, Any] | None,
    *,
    phase: str,
    status: str,
    error_code: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(existing_payload or {})
    _raw_progress = payload.get("_progress")
    progress = dict(_raw_progress) if isinstance(_raw_progress, dict) else {}
    _raw_steps = progress.get("steps")
    steps = dict(_raw_steps) if isinstance(_raw_steps, dict) else {}
    step_key = _step_key_for_phase(phase)
    _raw_step = steps.get(step_key)
    step = dict(_raw_step) if isinstance(_raw_step, dict) else {}
    updated_at = _now_iso()
    step.setdefault("started_at", str(step.get("updated_at") or updated_at))
    step_status = "completed" if phase == "warmup_ready" and status == "running" else status
    step.update(
        {
            "phase": phase,
            "status": step_status,
            "updated_at": updated_at,
            **_compact_progress_details(details),
        }
    )
    if error_code:
        step["error"] = error_code
    if step_status == "completed":
        step["success"] = True
        step.setdefault("completed_at", updated_at)
    if step_status == "failed":
        step["success"] = False
        step.setdefault("completed_at", updated_at)
    steps[step_key] = step

    if status != "failed" and step_key in PROGRESS_STEP_ORDER:
        current_idx = PROGRESS_STEP_ORDER.index(step_key)
        for previous_key in PROGRESS_STEP_ORDER[:current_idx]:
            previous = steps.get(previous_key)
            if not isinstance(previous, dict) or previous.get("status") != "running":
                continue
            normalised = dict(previous)
            normalised.update(
                {
                    "status": "completed",
                    "updated_at": updated_at,
                    "completed_at": updated_at,
                    "success": True,
                }
            )
            normalised.setdefault("started_at", str(previous.get("updated_at") or updated_at))
            steps[previous_key] = normalised
    progress.update({"phase": phase, "status": status, "steps": steps})
    payload["_progress"] = progress
    return payload


def _phase_is_terminal_for_artifacts(phase: str, status: str) -> bool:
    phase_value = str(phase or "").casefold()
    status_value = str(status or "").casefold()
    if status_value == "completed" and phase_value == "completed":
        return True
    if status_value in {"failed", "cancelled", "deleted"}:
        return True
    return phase_value.endswith("_failed") or phase_value in {
        "failed",
        "cancelled",
        "deleted",
        "worker_lost",
    }
