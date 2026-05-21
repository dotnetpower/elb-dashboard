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
        "completed_at",
        "duration_ms",
        "duration_source",
        "exit_code",
        "elastic_blast_submit_duration_ms",
        "k8s",
        "last_output",
        "log_line_count",
        "output",
        "skip_reason",
        "skipped",
        "source",
        "started_at",
        "terminal_duration_ms",
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
    compact_details = _compact_progress_details(details)
    existing_skipped = step.get("skipped") is True
    incoming_skipped = compact_details.get("skipped") is True
    preserve_skipped = (
        existing_skipped
        and status == "completed"
        and not incoming_skipped
        and compact_details.get("skipped") is not False
    )
    if phase == "completed" and "elastic_blast_submit_duration_ms" in compact_details:
        for key in ("last_output", "log_line_count", "output", "terminal_duration_ms"):
            compact_details.pop(key, None)
    if preserve_skipped:
        for key in (
            "duration_ms",
            "duration_source",
            "elastic_blast_submit_duration_ms",
            "exit_code",
            "last_output",
            "log_line_count",
            "output",
            "terminal_duration_ms",
            "timed_out",
        ):
            compact_details.pop(key, None)
    step_status = "completed" if phase == "warmup_ready" and status == "running" else status
    if compact_details.get("skipped") is True and step_status == "completed":
        step_status = "skipped"
    if preserve_skipped:
        step_status = "skipped"
    step.update(
        {
            "phase": phase,
            "status": step_status,
            "updated_at": updated_at,
            **compact_details,
        }
    )
    if error_code:
        step["error"] = error_code
    if step_status in {"completed", "skipped"}:
        step["success"] = True
        step.setdefault("completed_at", updated_at)
    if step_status == "failed":
        step["success"] = False
        step.setdefault("completed_at", updated_at)
    _normalise_step_duration(step)
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
                    "source": "legacy_inferred",
                }
            )
            normalised.setdefault("started_at", str(previous.get("updated_at") or updated_at))
            _normalise_step_duration(normalised)
            steps[previous_key] = normalised
    if status == "running" and phase == "results_pending":
        _synthesise_completed_runtime_steps(
            steps,
            details=details,
            completed_at=updated_at,
            complete_exporting=False,
        )
    if status == "completed" and phase == "completed":
        _synthesise_completed_runtime_steps(
            steps,
            details=details,
            completed_at=updated_at,
            complete_exporting=True,
        )
    progress.update({"phase": phase, "status": status, "steps": steps})
    payload["_progress"] = progress
    return payload


def _normalise_step_duration(step: dict[str, Any]) -> None:
    started_at = _parse_timestamp(step.get("started_at"))
    completed_at = _parse_timestamp(step.get("completed_at"))
    if started_at is None or completed_at is None:
        return
    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
    if duration_ms < 0:
        return
    step["duration_ms"] = duration_ms
    step.setdefault("duration_source", "timestamps")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _synthesise_completed_runtime_steps(
    steps: dict[str, Any],
    *,
    details: Mapping[str, Any],
    completed_at: str,
    complete_exporting: bool,
) -> None:
    k8s = details.get("k8s")
    k8s_payload = k8s if isinstance(k8s, Mapping) else {}
    runtime_started_at = _first_string(
        k8s_payload.get("started_at"),
        k8s_payload.get("start_time"),
        k8s_payload.get("job_started_at"),
    )
    runtime_completed_at = _first_string(
        k8s_payload.get("completed_at"),
        k8s_payload.get("completion_time"),
        k8s_payload.get("job_completed_at"),
    )

    submitting = steps.get("submitting")
    if isinstance(submitting, dict) and runtime_started_at:
        existing_completed_at = _parse_timestamp(submitting.get("completed_at"))
        runtime_start = _parse_timestamp(runtime_started_at)
        if runtime_start is not None and (
            existing_completed_at is None or existing_completed_at > runtime_start
        ):
            submitting["completed_at"] = runtime_started_at
            submitting["updated_at"] = runtime_started_at
            submitting["source"] = "server_checkpoint"
            _normalise_step_duration(submitting)
            steps["submitting"] = submitting

    if runtime_started_at and runtime_completed_at:
        running = dict(steps.get("running") if isinstance(steps.get("running"), dict) else {})
        running.update(
            {
                "phase": "running",
                "status": "completed",
                "started_at": runtime_started_at,
                "completed_at": runtime_completed_at,
                "updated_at": runtime_completed_at,
                "success": True,
                "k8s": dict(k8s_payload),
                "source": "k8s_runtime",
            }
        )
        _normalise_step_duration(running)
        running["duration_source"] = "k8s_runtime"
        steps["running"] = running

    if runtime_completed_at:
        exporting = dict(
            steps.get("exporting_results")
            if isinstance(steps.get("exporting_results"), dict)
            else {}
        )
        exporting.update(
            {
                "phase": "exporting_results",
                "status": "completed" if complete_exporting else "running",
                "started_at": runtime_completed_at
                if not complete_exporting and exporting.get("started_at") == completed_at
                else exporting.get("started_at") or runtime_completed_at,
                "updated_at": completed_at,
                "source": exporting.get("source")
                or (
                    "result_artifact_verification"
                    if complete_exporting
                    else "result_artifact_wait"
                ),
            }
        )
        if complete_exporting:
            exporting["completed_at"] = exporting.get("completed_at") or completed_at
            exporting["success"] = True
        else:
            exporting.pop("completed_at", None)
            exporting.pop("success", None)
            exporting.pop("duration_ms", None)
            exporting.pop("duration_source", None)
        _normalise_step_duration(exporting)
        steps["exporting_results"] = exporting

    for key, raw_step in list(steps.items()):
        if isinstance(raw_step, dict):
            _normalise_step_duration(raw_step)
            steps[key] = raw_step


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
