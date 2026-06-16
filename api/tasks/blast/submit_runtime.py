"""Submit-side runtime helpers — terminal exec streaming, K8s/Storage probes, result gating.

Responsibility: Drive the ``elastic-blast submit`` shell flow inside the terminal sidecar:
stream the command, emit progress checkpoints, refresh terminal/K8s status, and decide when
a completed phase is safe to surface to the UI.
Edit boundaries: All Celery state writes go through ``_blast._update_state`` / ``_blast._progress``
so test monkeypatches on the package propagate. Symbols are re-exported from ``api.tasks.blast``
so tests can ``monkeypatch.setattr(blast, "_has_parseable_result_artifact", …)``.
Key entry points: ``TerminalAzureLoginError``, ``TerminalKubeconfigError``,
``_ensure_terminal_azure_cli_login``, ``_ensure_terminal_kubeconfig_context``,
``_stream_submit_command``, ``_refresh_submit_terminal_status``, ``_has_parseable_result_artifact``,
``_discover_elastic_blast_job_id``, ``_gate_completed_submit_on_results``,
``_exception_detail_snippet``.
Risky contracts: ``_stream_submit_command`` enforces a 600s exec timeout and a 15s live-update
interval (``SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS``); widening either changes UI latency
and Container App billing. ``_gate_completed_submit_on_results`` downgrades ``completed`` to
``results_pending`` when no parseable result artifact is yet visible — the submit task relies
on this to avoid premature "completed" reporting. ``_stream_submit_command`` also retries the
submit once after stripping an OPTIONAL ``[cluster]`` param the terminal ``elastic-blast`` does
not recognise (``_OPTIONAL_STRIPPABLE_CFG_PARAMS``) so api/worker→terminal version skew cannot
hard-fail submit; required params are intentionally excluded so a genuinely invalid config still
fails loudly.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

# Optional, dashboard-added experimental ``[cluster]`` params that are pure
# optimisation hints. Across a deploy the api/worker image can start emitting a
# new param before the terminal toolchain base (which ships ``elastic-blast``)
# is rebuilt; the older CLI then rejects the param as
# ``Unrecognized configuration parameter`` and HARD-FAILS the whole submit even
# though dropping the hint only forgoes an optimisation. Strip such a param and
# retry the submit once so an optional hint can never break submit under version
# skew. Required params (e.g. ``exp-use-local-ssd``, which selects the only
# productised execution path) are intentionally NOT listed — an unrecognised
# required param must still fail loudly.
_OPTIONAL_STRIPPABLE_CFG_PARAMS: frozenset[str] = frozenset({"exp-skip-warmed-ssd-init"})

_UNRECOGNIZED_PARAM_RE = re.compile(
    r'Unrecognized configuration parameter "([^"]+)"', re.IGNORECASE
)


def _strip_optional_unrecognized_params(
    config_content: str, output: str
) -> tuple[str, list[str]]:
    """Return ``(config_without_optional_unrecognized_params, stripped_names)``.

    Scans ``output`` for elastic-blast "Unrecognized configuration parameter"
    errors whose parameter name is in ``_OPTIONAL_STRIPPABLE_CFG_PARAMS`` and
    removes the matching ``name = ...`` lines from ``config_content``. Names not
    in the strippable set are ignored, so a genuinely invalid config (an unknown
    required param, a typo) still fails loudly instead of being silently dropped.
    """
    names = {
        match.group(1)
        for match in _UNRECOGNIZED_PARAM_RE.finditer(output)
        if match.group(1) in _OPTIONAL_STRIPPABLE_CFG_PARAMS
    }
    if not names:
        return config_content, []
    kept: list[str] = []
    stripped: list[str] = []
    for line in config_content.splitlines():
        key = line.split("=", 1)[0].strip()
        if key in names:
            stripped.append(key)
            continue
        kept.append(line)
    new_content = "\n".join(kept)
    if config_content.endswith("\n"):
        new_content += "\n"
    return new_content, sorted(set(stripped))


class TerminalAzureLoginError(RuntimeError):
    """Raised when the terminal sidecar cannot acquire an Azure CLI identity."""


class TerminalKubeconfigError(RuntimeError):
    """Raised when the terminal sidecar cannot refresh its kubeconfig context."""


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


def _ensure_terminal_kubeconfig_context(
    terminal_run: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> None:
    """Refresh the terminal sidecar's kubeconfig for the target AKS cluster.

    elastic-blast invokes ``kubectl`` against the default context in
    ``~/.kube/config``; a stale context from a previously deleted cluster
    yields ``Unable to connect to the server`` mid-submit while elastic-blast
    still exits 0, leaving the dashboard with a phantom ``submitted`` row.
    ``~/.kube/config`` is shared per terminal sidecar; the per-(cluster,
    namespace) submit lock prevents same-cluster races, but unrelated
    cross-cluster submits could interleave. Acceptable trade-off pending a
    per-job ``--kubeconfig`` plumbing.
    """
    if not (subscription_id and resource_group and cluster_name):
        return
    result = terminal_run(
        argv=[
            "az",
            "aks",
            "get-credentials",
            "--subscription",
            subscription_id,
            "--resource-group",
            resource_group,
            "--name",
            cluster_name,
            "--overwrite-existing",
            "--only-show-errors",
        ],
        timeout_seconds=90,
    )
    if int(result.get("exit_code", 1) or 0) != 0:
        error = _result_error(result, None)
        raise TerminalKubeconfigError(error)


def _stream_submit_command(
    *,
    job_id: str,
    task: Any,
    config_content: str,
    progress_phase: str = "submitting",
) -> dict[str, Any]:
    """Stream ``elastic-blast submit`` and tolerate optional-param version skew.

    Runs the submit once. If it fails only because the terminal sidecar's
    ``elastic-blast`` rejects an OPTIONAL dashboard param as
    ``Unrecognized configuration parameter`` (api/worker emitted a hint the
    older terminal toolchain does not understand), the offending param is
    stripped and the submit is retried exactly once. This is safe because that
    rejection happens during config validation, before any Kubernetes resource
    is created, so no partial cluster state can leak across the retry.
    """
    result = _run_submit_stream_once(
        job_id=job_id,
        task=task,
        config_content=config_content,
        progress_phase=progress_phase,
    )
    if int(result.get("exit_code", 1) or 0) == 0:
        return result

    combined_output = "\n".join(
        str(value) for value in (result.get("stdout"), result.get("stderr")) if value
    )
    retry_content, stripped = _strip_optional_unrecognized_params(
        config_content, combined_output
    )
    if not stripped:
        return result

    LOGGER.warning(
        "submit: terminal elastic-blast rejected optional param(s) %s as "
        "unrecognized (toolchain version skew); retrying submit without them "
        "job_id=%s",
        ",".join(stripped),
        job_id,
    )
    retry_result = _run_submit_stream_once(
        job_id=job_id,
        task=task,
        config_content=retry_content,
        progress_phase=progress_phase,
    )
    retry_result["_stripped_optional_params"] = stripped
    return retry_result


def _run_submit_stream_once(
    *,
    job_id: str,
    task: Any,
    config_content: str,
    progress_phase: str = "submitting",
) -> dict[str, Any]:
    from api.services.blast.coordination import SUBMIT_EXEC_TIMEOUT_SECONDS
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
        timeout_seconds=SUBMIT_EXEC_TIMEOUT_SECONDS,
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


def _has_blast_success_marker(storage_account: str, job_id: str) -> bool:
    """True when the durable elastic-blast ``metadata/SUCCESS.txt`` marker exists.

    Authoritative completion signal that survives AKS cluster teardown and the
    ephemeral Celery result / Redis runtime cache (see
    ``api.services.blast.result_analytics.has_blast_success_marker``). Used by
    the stale-job reconciler as ground truth before declaring an unreachable,
    quiet job ``worker_lost``. Best-effort: returns ``False`` on any error.
    """
    try:
        from api.services.blast.result_analytics import has_blast_success_marker

        return has_blast_success_marker(storage_account, job_id)
    except Exception as exc:
        LOGGER.info("success marker check skipped job_id=%s: %s", job_id, type(exc).__name__)
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
    "TerminalKubeconfigError",
    "_discover_elastic_blast_job_id",
    "_ensure_terminal_azure_cli_login",
    "_ensure_terminal_kubeconfig_context",
    "_exception_detail_snippet",
    "_gate_completed_submit_on_results",
    "_has_blast_success_marker",
    "_has_parseable_result_artifact",
    "_refresh_submit_terminal_status",
    "_stream_submit_command",
)
