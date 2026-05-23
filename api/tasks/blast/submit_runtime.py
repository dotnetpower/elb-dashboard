"""Submit-side runtime helpers — terminal exec streaming, K8s/Storage probes, result gating.

Responsibility: Drive the ``elastic-blast submit`` shell flow inside the terminal sidecar:
stream the command, emit progress checkpoints, refresh terminal/K8s status, and decide when
a completed phase is safe to surface to the UI.
Edit boundaries: All Celery state writes go through ``_blast._update_state`` / ``_blast._progress``
so test monkeypatches on the package propagate. Symbols are re-exported from ``api.tasks.blast``
so tests can ``monkeypatch.setattr(blast, "_has_parseable_result_artifact", …)``.
Key entry points: ``TerminalAzureLoginError``, ``_ensure_terminal_azure_cli_login``,
``_stream_submit_command``, ``_refresh_submit_terminal_status``, ``_has_parseable_result_artifact``,
``_discover_elastic_blast_job_id``, ``_gate_completed_submit_on_results``,
``_exception_detail_snippet``.
Risky contracts: ``_stream_submit_command`` enforces a 600s exec timeout and a 15s live-update
interval (``SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS``); widening either changes UI latency
and Container App billing. ``_gate_completed_submit_on_results`` downgrades ``completed`` to
``results_pending`` when no parseable result artifact is yet visible — the submit task relies
on this to avoid premature "completed" reporting.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from api.tasks import blast as _blast
from api.tasks.blast.cli_parsing import (
    ELASTIC_BLAST_CFG_FILE,
    _elastic_blast_argv,
    _result_error,
)
from api.tasks.blast.progress import _tail_text
from api.tasks.blast.substeps import detect_submit_substep

LOGGER = logging.getLogger(__name__)

ERROR_SNIPPET_CHARS = 500
LIVE_OUTPUT_SNIPPET_CHARS = 8000
STDOUT_SNIPPET_CHARS = 1000
SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS = 15.0


class TerminalAzureLoginError(RuntimeError):
    """Raised when the terminal sidecar cannot acquire an Azure CLI identity."""


def _exception_detail_snippet(exc: Exception, *, limit: int = ERROR_SNIPPET_CHARS) -> str:
    detail = getattr(exc, "detail", None)
    if detail not in (None, ""):
        try:
            text = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
            return _blast._snippet(text, limit)
        except Exception:
            return _blast._snippet(detail, limit)
    return _blast._snippet(str(exc) or type(exc).__name__, limit)


def _ensure_terminal_azure_cli_login(terminal_run: Any) -> None:
    """Ensure shell-only ElasticBLAST calls have an Azure CLI account.

    The browser terminal remains interactive and user-owned. The programmatic
    exec server runs with its own ``AZURE_CONFIG_DIR`` and can safely acquire a
    short-lived managed-identity CLI session for API/Celery submissions.
    """
    account = terminal_run(
        argv=["az", "account", "show", "--query", "user.name", "--output", "tsv"],
        timeout_seconds=30,
    )
    if int(account.get("exit_code", 1) or 0) == 0:
        return

    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    argv = ["az", "login", "--identity"]
    if client_id:
        argv.extend(["--client-id", client_id])
    login = terminal_run(argv=argv, timeout_seconds=120)
    if int(login.get("exit_code", 1) or 0) != 0:
        error = _result_error(login, None)
        raise TerminalAzureLoginError(error)


def _stream_submit_command(
    *,
    job_id: str,
    task: Any,
    config_content: str,
    progress_phase: str = "submitting",
) -> dict[str, Any]:
    from api.services.sanitise import sanitise
    from api.services.terminal_exec import stream as terminal_stream

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    summary: dict[str, Any] = {"exit_code": 1, "duration_ms": 0, "timed_out": False}
    last_update = 0.0
    log_line_count = 0
    log_events: list[dict[str, Any]] = []
    current_substep: dict[str, Any] | None = None
    pending_substep: dict[str, Any] | None = None

    for item in terminal_stream(
        argv=_elastic_blast_argv("submit", job_id),
        stdin=config_content,
        stdin_file=ELASTIC_BLAST_CFG_FILE,
        timeout_seconds=600,
    ):
        if "line" in item:
            stream_name = str(item.get("stream") or "stdout")
            line = sanitise(str(item.get("line") or ""))
            if stream_name == "stderr":
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)
            try:
                from api.services.job_logs.event_bus import publish_job_log_event

                publish_job_log_event(
                    job_id,
                    source="terminal_exec",
                    phase=progress_phase,
                    stream=stream_name,
                    line=line,
                )
            except Exception as exc:
                LOGGER.debug("submit live log publish skipped job_id=%s: %s", job_id, exc)
            log_line_count += 1
            log_events.append({"stream": stream_name, "line": line, "index": log_line_count})
            substep_candidate = detect_submit_substep(line)
            if substep_candidate is not None and (
                current_substep is None
                or substep_candidate["index"] > int(current_substep.get("index") or 0)
            ):
                current_substep = substep_candidate
                pending_substep = substep_candidate
            now = time.monotonic()
            interval_elapsed = (
                now - last_update >= SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS
            )
            if pending_substep is not None or interval_elapsed:
                live_output = _tail_text(stdout_lines + stderr_lines)
                progress_kwargs: dict[str, Any] = {
                    "last_output": live_output,
                    "log_line_count": log_line_count,
                }
                if current_substep is not None:
                    progress_kwargs["submit_progress"] = dict(current_substep)
                _blast._progress(task, progress_phase, **progress_kwargs)
                _blast._update_state(
                    job_id,
                    progress_phase,
                    status="running",
                    event="submit_log",
                    last_output=live_output,
                    log_line_count=log_line_count,
                    submit_progress=dict(current_substep) if current_substep is not None else None,
                )
                last_update = now
                pending_substep = None
            continue
        summary = dict(item)

    stdout = "\n".join(stdout_lines)
    stderr = "\n".join(stderr_lines)
    return {
        **summary,
        "stdout": stdout,
        "stderr": stderr,
        "log_line_count": log_line_count,
        "_log_events": log_events,
        "_submit_progress": dict(current_substep) if current_substep is not None else None,
    }


def _refresh_submit_terminal_status(
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    k8s_job_id: str | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        k8s = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=k8s_job_id or job_id,
        )
    except Exception as exc:
        LOGGER.info("submit terminal status refresh skipped job_id=%s: %s", job_id, exc)
        return "submitted", "running", None

    k8s_status = str(k8s.get("status") or "")
    if k8s_status == "completed":
        return "completed", "completed", k8s
    if k8s_status == "failed":
        return "failed", "failed", k8s
    return "submitted", "running", k8s


def _has_parseable_result_artifact(storage_account: str, job_id: str) -> bool:
    try:
        from api.services.blast.result_analytics import list_parseable_result_blobs

        return bool(list_parseable_result_blobs(storage_account, job_id))
    except Exception as exc:
        LOGGER.info("result artifact check skipped job_id=%s: %s", job_id, type(exc).__name__)
        return False


def _discover_elastic_blast_job_id(storage_account: str, job_id: str) -> str:
    if not storage_account or not job_id:
        return ""
    try:
        from api.services import get_credential
        from api.services.storage.data import _blob_service

        container = _blob_service(get_credential(), storage_account).get_container_client("results")
        prefix = f"{job_id}/job-"
        for blob in container.list_blobs(name_starts_with=prefix):
            name = str(blob.name or "")
            parts = name.split("/", 2)
            if len(parts) >= 2 and parts[1].startswith("job-"):
                return parts[1]
    except Exception as exc:
        LOGGER.info(
            "elastic blast job id discovery skipped job_id=%s: %s", job_id, type(exc).__name__
        )
    return ""


def _gate_completed_submit_on_results(
    *,
    job_id: str,
    storage_account: str,
    phase: str,
    status: str,
) -> tuple[str, str]:
    if (
        phase == "completed"
        and status == "completed"
        and not _blast._has_parseable_result_artifact(
            storage_account,
            job_id,
        )
    ):
        return "results_pending", "running"
    return phase, status


__all__ = (
    "ERROR_SNIPPET_CHARS",
    "LIVE_OUTPUT_SNIPPET_CHARS",
    "STDOUT_SNIPPET_CHARS",
    "SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS",
    "TerminalAzureLoginError",
    "_discover_elastic_blast_job_id",
    "_ensure_terminal_azure_cli_login",
    "_exception_detail_snippet",
    "_gate_completed_submit_on_results",
    "_has_parseable_result_artifact",
    "_refresh_submit_terminal_status",
    "_stream_submit_command",
)
