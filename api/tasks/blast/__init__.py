"""BLAST Celery tasks for submit, cancel, status, and reconciliation.

Responsibility: BLAST Celery tasks for submit, cancel, status, and reconciliation
Edit boundaries: Keep long-running side effects here; route handlers should enqueue tasks and
persist state.
Key entry points: `TerminalAzureLoginError`, `_now_iso`, `_snippet`, `submit`,
`merge_split_results`, `cancel`
Risky contracts: Tasks should be idempotent, retry-aware, and write progress/state checkpoints.
Validation: `uv run pytest -q api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import UTC
from typing import Any, cast

from celery import shared_task

from api.services import blast_task_config as _blast_task_config
from api.services.blast_db_metadata import extract_db_name, resolve_db_metadata
from api.services.blast_oracles import (
    upload_db_order_oracle_pointer_if_available,
    upload_tie_order_oracle_if_present,
)
from api.services.blast_task_config import WarmupNotReadyError
from api.services.query_grouping import QuerySplitExecutionPlan
from api.services.terminal_exec import TerminalExecError
from api.tasks.blast.progress import (
    _merge_progress_payload,
    _phase_is_terminal_for_artifacts,
    _tail_text,
)
from api.tasks.blast.split_constants import (
    QUERY_FASTA_READ_MAX_BYTES,
    SPLIT_CHILD_CANCELLED_STATUSES,
    SPLIT_CHILD_KNOWN_STATUSES,
    SPLIT_CHILD_MERGE_REPORT_BLOB,
    SPLIT_CHILD_MERGED_RESULT_BLOB,
    SPLIT_CHILD_OPTION_ALLOWLIST,
    SPLIT_MERGE_REPORT_MAX_BYTES,
    SPLIT_PARENT_MANIFEST_BLOB,
    SPLIT_UPLOAD_VERIFY_BYTES,
    STRICT_TIE_ORDER_MIN_TARGET_SEQS,
)
from api.tasks.blast.submit_lock import (
    BLAST_SUBMIT_LOCK_KEY_PREFIX,
    BLAST_SUBMIT_LOCK_TTL_SECONDS,
    acquire_submit_lock,
    release_submit_lock,
    submit_lock_key,
)
from api.tasks.blast.submit_logs import (
    SUBMIT_LOG_CHUNK_EVENT_COUNT,
    persist_submit_log_events,
)
from api.tasks.blast.substeps import (
    SUBMIT_SUBSTEP_PATTERNS,
    SUBMIT_SUBSTEP_TOTAL,
    detect_submit_substep,
)

LOGGER = logging.getLogger(__name__)

STDOUT_SNIPPET_CHARS = 1000
ERROR_SNIPPET_CHARS = 500
LIVE_OUTPUT_SNIPPET_CHARS = 8000
RETRYABLE_ERROR_CATEGORIES = {"transient", "capacity", "conflict"}
RETRYABLE_EXIT_CODES = {8, 10}
SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS = 15.0
ELASTIC_BLAST_JOB_ID_RE = re.compile(r"/results/[^/]+/(job-[A-Za-z0-9_-]+)")

# Backwards-compatible private aliases so callers inside this module that still
# use the underscore-prefixed names continue to work. The canonical home for
# these symbols is now ``api.tasks.blast.submit_lock`` / ``submit_logs`` /
# ``substeps``.
_submit_lock_key = submit_lock_key
_acquire_submit_lock = acquire_submit_lock
_release_submit_lock = release_submit_lock
_persist_submit_log_events = persist_submit_log_events
_detect_submit_substep = detect_submit_substep

__all__ = (
    "BLAST_SUBMIT_LOCK_KEY_PREFIX",
    "BLAST_SUBMIT_LOCK_TTL_SECONDS",
    "SUBMIT_LOG_CHUNK_EVENT_COUNT",
    "SUBMIT_SUBSTEP_PATTERNS",
    "SUBMIT_SUBSTEP_TOTAL",
    "acquire_submit_lock",
    "detect_submit_substep",
    "persist_submit_log_events",
    "release_submit_lock",
    "submit_lock_key",
)


class TerminalAzureLoginError(RuntimeError):
    """Raised when the terminal sidecar cannot acquire an Azure CLI identity."""


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _snippet(value: object, limit: int = ERROR_SNIPPET_CHARS) -> str:
    return _blast_task_config.snippet(value, limit)


def _exception_detail_snippet(exc: Exception, *, limit: int = ERROR_SNIPPET_CHARS) -> str:
    detail = getattr(exc, "detail", None)
    if detail not in (None, ""):
        try:
            text = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
            return _snippet(text, limit)
        except Exception:
            return _snippet(detail, limit)
    return _snippet(str(exc) or type(exc).__name__, limit)


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


def _storage_url(storage_account: str, container: str, path: str = "") -> str:
    return _blast_task_config.storage_url(storage_account, container, path)


def _relative_blob_path(value: str, label: str) -> str:
    return _blast_task_config.relative_blob_path(value, label)


def _normalise_query_url(storage_account: str, query_file: str) -> str:
    return _blast_task_config.normalise_query_url(storage_account, query_file)


def _query_blob_path_from_query_file(*, storage_account: str, query_file: str) -> str:
    """Return a safe blob path in the queries container for an original query file."""
    return _blast_task_config.query_blob_path_from_query_file(
        storage_account=storage_account,
        query_file=query_file,
    )


def _normalise_database_url(storage_account: str, database: str) -> str:
    return _blast_task_config.normalise_database_url(storage_account, database)


def _results_job_url(storage_account: str, job_id: str) -> str:
    return _blast_task_config.results_job_url(storage_account, job_id)


def _build_config_content(
    *,
    job_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str = "blastn",
    database: str = "",
    query_file: str = "",
    options: Mapping[str, Any] | None = None,
) -> str:
    return _blast_task_config.build_config_content(
        job_id=job_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        storage_account=storage_account,
        program=program,
        database=database,
        query_file=query_file,
        options=options,
        metadata_resolver=resolve_db_metadata,
    )


def _option_enabled(options: Mapping[str, Any], key: str) -> bool:
    return _blast_task_config.option_enabled(options, key)


def _metadata_has_prepared_shard_layout(db_name: str, meta: Mapping[str, Any]) -> bool:
    return _blast_task_config.metadata_has_prepared_shard_layout(db_name, meta)


def _disable_sharding_options(options: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return _blast_task_config.disable_sharding_options(options)


def _expand_strict_tie_order_candidate_pool(
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(options, Mapping):
        return None if options is None else dict(options)
    has_oracle = bool(
        options.get("tie_order_oracle_accessions") or options.get("tie_order_oracle_text")
    )
    if not has_oracle or not _option_enabled(options, "tie_order_oracle_strict"):
        return cast(dict[str, Any], options)
    current_raw = options.get("max_target_seqs")
    try:
        current = int(current_raw) if current_raw not in (None, "") else 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        current = 0
    if current >= STRICT_TIE_ORDER_MIN_TARGET_SEQS:
        return dict(options)
    expanded = dict(options)
    expanded["requested_max_target_seqs"] = current_raw
    expanded["max_target_seqs"] = STRICT_TIE_ORDER_MIN_TARGET_SEQS
    return expanded


def _suppress_sharding_for_unsharded_database(
    *,
    storage_account: str,
    database: str,
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    return _blast_task_config.suppress_sharding_for_unsharded_database(
        storage_account=storage_account,
        database=database,
        options=options,
        metadata_resolver=resolve_db_metadata,
    )


def _submit_requires_node_warmup(options: Mapping[str, Any] | None) -> bool:
    return _blast_task_config.submit_requires_node_warmup(options)


def _ensure_node_warmup_ready_for_submit(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    database: str,
    storage_account: str = "",
    options: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    return _blast_task_config.ensure_node_warmup_ready_for_submit(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        database=database,
        storage_account=storage_account,
        options=options,
        metadata_resolver=resolve_db_metadata,
    )


def _upload_split_query_files(
    *,
    storage_account: str,
    plan: QuerySplitExecutionPlan,
) -> list[dict[str, Any]]:
    """Upload split query FASTA payloads and return state-safe metadata."""
    from api.services import get_credential
    from api.services.storage_data import read_blob_text, upload_group_fasta

    credential = get_credential()
    uploaded: list[dict[str, Any]] = []
    for group in plan.groups:
        blob_url = upload_group_fasta(
            credential,
            storage_account,
            group.query_blob_path,
            group.query_fasta,
        )
        uploaded_prefix = read_blob_text(
            credential,
            storage_account,
            "queries",
            group.query_blob_path,
            max_bytes=SPLIT_UPLOAD_VERIFY_BYTES,
        )
        if not uploaded_prefix.strip().startswith(">"):
            raise ValueError(f"split query upload verification failed: {group.query_blob_path}")
        uploaded.append(
            {
                "group_id": group.group_id,
                "child_job_id": group.child_job_id,
                "effective_search_space": group.effective_search_space,
                "query_blob_path": group.query_blob_path,
                "query_file": group.query_file,
                "query_blob_url": blob_url,
                "query_fasta_bytes": len(group.query_fasta.encode("utf-8")),
                "options": group.options,
            }
        )
    return uploaded


def _split_child_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return only option keys safe to forward to a split child submit."""
    unsafe = sorted(set(options) - SPLIT_CHILD_OPTION_ALLOWLIST)
    if unsafe:
        raise ValueError(f"split child options contain unsupported keys: {', '.join(unsafe)}")
    return dict(options)


def _requires_split_parent_submission(options: Mapping[str, Any] | None) -> bool:
    """Return True for public submits that must fan out by query group."""
    if not isinstance(options, Mapping):
        return False
    from api.services.sharding_precision import (
        normalize_sharding_mode,
        query_effective_search_spaces,
        uniform_query_effective_search_space,
    )

    opts = dict(options)
    if normalize_sharding_mode(opts) != "precise":
        return False
    query_count_raw = opts.get("query_count")
    try:
        query_count = int(query_count_raw) if query_count_raw is not None else 0
    except (TypeError, ValueError):
        return False
    spaces = query_effective_search_spaces(opts.get("query_effective_search_spaces"))
    return (
        query_count > 1
        and len(spaces) == query_count
        and uniform_query_effective_search_space(opts, query_count) is None
    )


def _build_split_child_submit_plan(
    *,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str,
    database: str,
    uploaded_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build state-safe child submit inputs for already-uploaded split queries."""
    children: list[dict[str, Any]] = []
    for group in uploaded_groups:
        child_job_id = str(group.get("child_job_id") or "")
        query_file = str(group.get("query_file") or "")
        options = group.get("options")
        if not child_job_id or not query_file or not isinstance(options, Mapping):
            raise ValueError("uploaded split group is missing child_job_id, query_file, or options")

        child_options = _split_child_options(options)
        config_content = _build_config_content(
            job_id=child_job_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            storage_account=storage_account,
            program=program,
            database=database,
            query_file=query_file,
            options=child_options,
        )
        children.append(
            {
                "group_id": group.get("group_id"),
                "child_job_id": child_job_id,
                "query_file": query_file,
                "query_blob_path": group.get("query_blob_path"),
                "query_blob_url": group.get("query_blob_url"),
                "query_fasta_bytes": group.get("query_fasta_bytes"),
                "effective_search_space": group.get("effective_search_space"),
                "argv": _elastic_blast_argv("submit", child_job_id),
                "config_content": config_content,
                "options": child_options,
            }
        )
    return children


def _child_state_payload(child: Mapping[str, Any]) -> dict[str, Any]:
    """Return child metadata safe to persist in Table Storage/history."""
    return {
        "group_id": child.get("group_id"),
        "query_file": child.get("query_file"),
        "query_blob_path": child.get("query_blob_path"),
        "query_blob_url": child.get("query_blob_url"),
        "query_fasta_bytes": child.get("query_fasta_bytes"),
        "effective_search_space": child.get("effective_search_space"),
        "options": child.get("options"),
    }


def _dispatch_split_child_submits(
    *,
    parent_job_id: str,
    owner_oid: str,
    tenant_id: str,
    children: list[dict[str, Any]],
    terminal_run: Any | None = None,
) -> list[dict[str, Any]]:
    """Create child state records and submit each child ElasticBLAST config."""
    from api.services.state_repo import JobState, JobStateRepository

    if terminal_run is None:
        from api.services.terminal_exec import run as _terminal_run

        terminal_run = _terminal_run

    repo = JobStateRepository()
    dispatched: list[dict[str, Any]] = []
    for child in children:
        child_job_id = str(child.get("child_job_id") or "")
        config_content = str(child.get("config_content") or "")
        argv = child.get("argv")
        if not child_job_id or not config_content or not isinstance(argv, list):
            raise ValueError("child submit plan is missing child_job_id, config_content, or argv")

        payload = _child_state_payload(child)
        try:
            repo.create(
                JobState(
                    job_id=child_job_id,
                    type="blast-child",
                    status="queued",
                    phase="queued",
                    owner_oid=owner_oid,
                    tenant_id=tenant_id,
                    parent_job_id=parent_job_id,
                    payload=payload,
                )
            )
        except Exception as exc:
            LOGGER.info(
                "child state create skipped job_id=%s: %s",
                child_job_id,
                type(exc).__name__,
            )

        repo.update(child_job_id, status="running", phase="submitting")
        result = terminal_run(
            argv=argv,
            stdin=config_content,
            stdin_file=ELASTIC_BLAST_CFG_FILE,
            timeout_seconds=600,
        )
        payload_json = _last_json(str(result.get("stdout", "")))
        exit_code = int(result.get("exit_code", 1) or 0)
        if exit_code == 0:
            phase, status = _submit_success_status(payload_json)
            repo.update(child_job_id, status=status, phase=phase)
            repo.append_history(
                child_job_id,
                phase,
                {
                    "parent_job_id": parent_job_id,
                    "group_id": child.get("group_id"),
                    "decision": (payload_json or {}).get("decision"),
                    "output": _snippet(result.get("stdout"), STDOUT_SNIPPET_CHARS),
                },
            )
            dispatched.append(
                {
                    "child_job_id": child_job_id,
                    "group_id": child.get("group_id"),
                    "status": status,
                    "phase": phase,
                    "decision": (payload_json or {}).get("decision", "accepted"),
                }
            )
            continue

        error = _result_error(result, payload_json)
        repo.update(child_job_id, status="failed", phase="submit_failed", error_code=error)
        repo.append_history(
            child_job_id,
            "submit_failed",
            {"parent_job_id": parent_job_id, "group_id": child.get("group_id"), "error": error},
        )
        dispatched.append(
            {
                "child_job_id": child_job_id,
                "group_id": child.get("group_id"),
                "status": "failed",
                "phase": "submit_failed",
                "error": error,
            }
        )
    return dispatched


def _run_split_parent_submission(
    *,
    parent_job_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str,
    database: str,
    query_fasta_text: str,
    query_effective_search_spaces: object,
    options: dict[str, Any] | None,
    owner_oid: str,
    tenant_id: str,
    terminal_run: Any | None = None,
) -> dict[str, Any]:
    """Run split-query preparation and child submission for a parent job.

    Raw FASTA is intentionally accepted only as an in-memory argument and is
    never included in the returned payload, state updates, or history records.
    """
    from api.services.query_grouping import build_query_split_execution_plan
    from api.services.query_metadata import parse_fasta_metadata

    _update_state(parent_job_id, "splitting_queries", event="split_queries_started")
    metadata = parse_fasta_metadata(query_fasta_text)
    split_plan = build_query_split_execution_plan(
        parent_job_id=parent_job_id,
        metadata=metadata,
        query_effective_search_spaces_value=query_effective_search_spaces,
        base_options=options,
    )
    if not split_plan.requires_split:
        raise ValueError("split parent submission requires mixed query effective search spaces")

    uploaded_groups = _upload_split_query_files(
        storage_account=storage_account,
        plan=split_plan,
    )
    children = _build_split_child_submit_plan(
        resource_group=resource_group,
        cluster_name=cluster_name,
        storage_account=storage_account,
        program=program,
        database=database,
        uploaded_groups=uploaded_groups,
    )
    dispatched = _dispatch_split_child_submits(
        parent_job_id=parent_job_id,
        owner_oid=owner_oid,
        tenant_id=tenant_id,
        children=children,
        terminal_run=terminal_run,
    )
    failed = [child for child in dispatched if child.get("status") == "failed"]
    parent_phase = "split_children_failed" if failed else "split_children_submitted"
    parent_status = "failed" if failed else "running"
    _update_state(
        parent_job_id,
        parent_phase,
        status=parent_status,
        event=parent_phase,
        child_count=len(dispatched),
        failed_child_count=len(failed),
        children=dispatched,
    )
    return {
        "job_id": parent_job_id,
        "status": parent_status,
        "phase": parent_phase,
        "query_count": metadata.query_count,
        "child_count": len(dispatched),
        "failed_child_count": len(failed),
        "children": dispatched,
    }


def _run_storage_query_split_parent_submission(
    *,
    parent_job_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str,
    database: str,
    query_file: str,
    query_effective_search_spaces: object,
    options: dict[str, Any] | None,
    owner_oid: str,
    tenant_id: str,
    terminal_run: Any | None = None,
) -> dict[str, Any]:
    """Read the original query FASTA from Storage and dispatch split children.

    The ``query_file`` must point to the original user-uploaded blob in the
    ``queries`` container. Raw FASTA is never returned, logged, or persisted.
    """
    from azure.core.exceptions import ResourceNotFoundError

    from api.services import get_credential
    from api.services.storage_data import read_blob_text

    query_blob_path = _query_blob_path_from_query_file(
        storage_account=storage_account,
        query_file=query_file,
    )
    _update_state(
        parent_job_id,
        "reading_split_query",
        event="split_query_read_started",
        query_file=query_blob_path,
        max_bytes=QUERY_FASTA_READ_MAX_BYTES,
    )

    query_fasta_text: str | None = None
    try:
        try:
            query_fasta_text = read_blob_text(
                get_credential(),
                storage_account,
                "queries",
                query_blob_path,
                max_bytes=QUERY_FASTA_READ_MAX_BYTES + 1,
            )
        except ResourceNotFoundError as exc:
            raise ValueError(
                f"query_file not found in queries container: {query_blob_path}"
            ) from exc

        if len(query_fasta_text.encode("utf-8")) > QUERY_FASTA_READ_MAX_BYTES:
            raise ValueError("query_file is too large for split planning")
        if not query_fasta_text.strip().startswith(">"):
            raise ValueError("query_file does not appear to be FASTA format")
    except Exception as exc:
        _update_state(
            parent_job_id,
            "split_query_invalid",
            status="failed",
            error_code=_snippet(exc),
            query_file=query_blob_path,
        )
        query_fasta_text = None
        raise

    try:
        return _run_split_parent_submission(
            parent_job_id=parent_job_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            storage_account=storage_account,
            program=program,
            database=database,
            query_fasta_text=query_fasta_text,
            query_effective_search_spaces=query_effective_search_spaces,
            options=options,
            owner_oid=owner_oid,
            tenant_id=tenant_id,
            terminal_run=terminal_run,
        )
    finally:
        query_fasta_text = None


def _split_child_state_summary(child: Any) -> dict[str, Any]:
    """Return child state fields safe to include in parent aggregation history."""
    payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
    return {
        "job_id": getattr(child, "job_id", ""),
        "status": getattr(child, "status", ""),
        "phase": getattr(child, "phase", None),
        "error_code": getattr(child, "error_code", None),
        "group_id": payload.get("group_id"),
        "query_file": payload.get("query_file"),
        "query_fasta_bytes": payload.get("query_fasta_bytes"),
        "effective_search_space": payload.get("effective_search_space"),
    }


def _aggregate_split_child_states(
    *,
    parent_job_id: str,
    expected_child_count: int | None = None,
    child_limit: int = 1000,
    repo: Any | None = None,
    update_parent: bool = True,
) -> dict[str, Any]:
    """Aggregate split-child states and move the parent to a safe intermediate phase.

    This helper never marks the parent job ``completed``. All children reaching
    ``completed`` only means the parent is ready for the future merge step.
    """
    if expected_child_count is not None and expected_child_count < 0:
        raise ValueError("expected_child_count must be non-negative")
    if child_limit <= 0:
        raise ValueError("child_limit must be positive")

    if repo is None:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()

    children = list(repo.list_children(parent_job_id, limit=child_limit))
    if len(children) >= child_limit:
        raise ValueError("split child query may be truncated by child_limit")
    if not children:
        raise ValueError(f"split parent has no child jobs: {parent_job_id}")
    if expected_child_count is not None and len(children) > expected_child_count:
        raise ValueError("split parent has more child jobs than expected")

    counts = {status: 0 for status in sorted(SPLIT_CHILD_KNOWN_STATUSES)}
    summaries: list[dict[str, Any]] = []
    for child in children:
        status = str(getattr(child, "status", "") or "").lower()
        if status not in SPLIT_CHILD_KNOWN_STATUSES:
            child_id = str(getattr(child, "job_id", "") or "<unknown>")
            raise ValueError(f"split child {child_id} has unknown status: {status}")
        counts[status] += 1
        summaries.append(_split_child_state_summary(child))

    missing_child_count = 0
    if expected_child_count is not None:
        missing_child_count = max(0, expected_child_count - len(children))

    failed_children = [child for child in summaries if child["status"] == "failed"]
    cancelled_children = [
        child for child in summaries if child["status"] in SPLIT_CHILD_CANCELLED_STATUSES
    ]

    if failed_children:
        parent_status = "failed"
        parent_phase = "split_children_failed"
        ready_for_merge = False
    elif cancelled_children:
        parent_status = "cancelled"
        parent_phase = "split_children_cancelled"
        ready_for_merge = False
    elif missing_child_count:
        parent_status = "running"
        parent_phase = "split_children_aggregating"
        ready_for_merge = False
    elif counts["completed"] == len(children):
        parent_status = "running"
        parent_phase = "split_children_merge_ready"
        ready_for_merge = True
    else:
        parent_status = "running"
        parent_phase = "split_children_aggregating"
        ready_for_merge = False

    summary = {
        "parent_job_id": parent_job_id,
        "status": parent_status,
        "phase": parent_phase,
        "ready_for_merge": ready_for_merge,
        "child_count": len(children),
        "expected_child_count": expected_child_count,
        "missing_child_count": missing_child_count,
        "children_by_status": counts,
        "failed_children": failed_children,
        "cancelled_children": cancelled_children,
        "children": summaries,
    }
    if update_parent:
        _update_state(
            parent_job_id,
            parent_phase,
            status=parent_status,
            event="split_children_aggregated",
            ready_for_merge=ready_for_merge,
            child_count=len(children),
            expected_child_count=expected_child_count,
            missing_child_count=missing_child_count,
            children_by_status=counts,
            failed_children=failed_children,
            cancelled_children=cancelled_children,
            children=summaries,
        )
    return summary


def _split_child_result_paths(child_job_id: str) -> dict[str, str]:
    child_id = _relative_blob_path(child_job_id, "child_job_id")
    return {
        "merged_result_path": f"{child_id}/{SPLIT_CHILD_MERGED_RESULT_BLOB}",
        "merge_report_path": f"{child_id}/{SPLIT_CHILD_MERGE_REPORT_BLOB}",
    }


def _parent_split_result_paths(parent_job_id: str) -> dict[str, str]:
    parent_id = _relative_blob_path(parent_job_id, "parent_job_id")
    return {
        "merged_result_path": f"{parent_id}/{SPLIT_CHILD_MERGED_RESULT_BLOB}",
        "merge_report_path": f"{parent_id}/{SPLIT_CHILD_MERGE_REPORT_BLOB}",
        "manifest_path": f"{parent_id}/{SPLIT_PARENT_MANIFEST_BLOB}",
    }


def _result_blob_map(
    *,
    storage_account: str,
    prefix: str,
    credential: Any | None = None,
) -> dict[str, dict[str, Any]]:
    if credential is None:
        from api.services import get_credential

        credential = get_credential()
    from api.services.storage_data import list_result_blobs

    return {
        str(blob.get("name")): blob
        for blob in list_result_blobs(credential, storage_account, "results", prefix)
    }


def _parent_split_result_artifacts_present(
    *,
    parent_job_id: str,
    storage_account: str,
    credential: Any | None = None,
) -> dict[str, Any]:
    paths = _parent_split_result_paths(parent_job_id)
    blobs = _result_blob_map(
        storage_account=storage_account,
        prefix=f"{parent_job_id}/",
        credential=credential,
    )
    present = {
        "merged_result": paths["merged_result_path"] in blobs,
        "merge_report": paths["merge_report_path"] in blobs,
        "manifest": paths["manifest_path"] in blobs,
    }
    return {
        "all_present": all(present.values()),
        "present": present,
        "paths": paths,
        "blobs": {name: blobs[name] for name in paths.values() if name in blobs},
    }


def _verify_split_child_result_artifacts(
    *,
    parent_job_id: str,
    storage_account: str,
    children: list[Any],
    credential: Any | None = None,
) -> dict[str, Any]:
    """Verify every completed split child has finalizer output artifacts."""
    if credential is None:
        from api.services import get_credential

        credential = get_credential()

    statuses: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for child in children:
        child_job_id = str(getattr(child, "job_id", "") or "")
        status = str(getattr(child, "status", "") or "").lower()
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        if not child_job_id:
            raise ValueError("split child state is missing job_id")
        if status != "completed":
            raise ValueError(f"split child {child_job_id} is not completed: {status}")

        paths = _split_child_result_paths(child_job_id)
        blobs = _result_blob_map(
            storage_account=storage_account,
            prefix=f"{child_job_id}/",
            credential=credential,
        )
        has_merged = paths["merged_result_path"] in blobs
        has_report = paths["merge_report_path"] in blobs
        status_item = {
            "parent_job_id": parent_job_id,
            "child_job_id": child_job_id,
            "group_id": payload.get("group_id"),
            "merged_result_path": paths["merged_result_path"],
            "merge_report_path": paths["merge_report_path"],
            "has_merged_result": has_merged,
            "has_merge_report": has_report,
            "merged_result_size": blobs.get(paths["merged_result_path"], {}).get("size"),
            "merge_report_size": blobs.get(paths["merge_report_path"], {}).get("size"),
        }
        statuses.append(status_item)
        if not (has_merged and has_report):
            missing_bits = []
            if not has_merged:
                missing_bits.append(SPLIT_CHILD_MERGED_RESULT_BLOB)
            if not has_report:
                missing_bits.append(SPLIT_CHILD_MERGE_REPORT_BLOB)
            missing.append(
                {
                    "child_job_id": child_job_id,
                    "group_id": payload.get("group_id"),
                    "missing": missing_bits,
                }
            )

    return {
        "parent_job_id": parent_job_id,
        "all_artifacts_present": not missing,
        "missing_artifacts": missing,
        "children": statuses,
    }


def _load_split_child_merge_reports(
    *,
    storage_account: str,
    children: list[Any],
    credential: Any,
) -> list[dict[str, Any]]:
    from api.services.storage_data import read_blob_text

    reports: list[dict[str, Any]] = []
    for child in children:
        child_job_id = str(getattr(child, "job_id", "") or "")
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        path = _split_child_result_paths(child_job_id)["merge_report_path"]
        raw = read_blob_text(
            credential,
            storage_account,
            "results",
            path,
            max_bytes=SPLIT_MERGE_REPORT_MAX_BYTES,
        )
        try:
            report = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid child merge report JSON: {child_job_id}") from exc
        if not isinstance(report, dict):
            raise ValueError(f"child merge report must be an object: {child_job_id}")
        reports.append(
            {
                "child_job_id": child_job_id,
                "group_id": payload.get("group_id"),
                "report": report,
            }
        )
    return reports


def _aggregate_split_merge_reports(
    *,
    parent_job_id: str,
    child_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine child finalizer reports into one parent-level report."""
    warnings: list[str] = []
    max_target_values: set[int] = set()
    formats: set[str] = set()
    precision_levels: set[str] = set()
    totals = {
        "queries": 0,
        "total_input_hits": 0,
        "total_output_hits": 0,
        "unsupported_rows": 0,
        "unsupported_records": 0,
        "malformed_xml_count": 0,
        "total_input_hsps": 0,
        "total_output_hsps": 0,
        "tie_break_count": 0,
        "num_shards": 0,
    }
    child_items: list[dict[str, Any]] = []
    for item in child_reports:
        report = item["report"]
        outfmt_value = report.get("outfmt")
        report_format = (
            "blast_xml"
            if str(outfmt_value).strip() == "5"
            else str(report.get("format") or "blast_tabular")
        )
        formats.add(report_format)
        precision_level = report.get("precision_level")
        if isinstance(precision_level, str) and precision_level:
            precision_levels.add(precision_level)
        for key in totals:
            raw_value = report.get(key, 0)
            if isinstance(raw_value, (int, float)):
                totals[key] += int(raw_value)
        max_target = report.get("max_target_seqs")
        if isinstance(max_target, (int, float)) and max_target > 0:
            max_target_values.add(int(max_target))
        for warning in report.get("warnings", []):
            if isinstance(warning, str) and warning not in warnings:
                warnings.append(warning)
        child_items.append(
            {
                "child_job_id": item.get("child_job_id"),
                "group_id": item.get("group_id"),
                "queries": report.get("queries", 0),
                "total_input_hits": report.get("total_input_hits", 0),
                "total_output_hits": report.get("total_output_hits", 0),
                "unsupported_rows": report.get("unsupported_rows", 0),
                "unsupported_records": report.get("unsupported_records", 0),
                "malformed_xml_count": report.get("malformed_xml_count", 0),
                "total_input_hsps": report.get("total_input_hsps", 0),
                "total_output_hsps": report.get("total_output_hsps", 0),
                "tie_break_count": report.get("tie_break_count", 0),
                "num_shards": report.get("num_shards", 0),
                "format": report_format,
                "warnings": report.get("warnings", []),
            }
        )
    if len(max_target_values) > 1:
        warnings.append("child merge reports used different max_target_seqs values")
    if len(formats) > 1:
        raise ValueError("split child merge reports used different output formats")
    if len(precision_levels) > 1:
        raise ValueError("split child merge reports used different precision levels")
    report_format = next(iter(formats), "blast_tabular")
    outfmt = 5 if report_format == "blast_xml" else 6

    return {
        "precision_level": (
            "split_query_child_finalizer_xml_concat"
            if report_format == "blast_xml"
            else "split_query_child_finalizer_concat"
        ),
        "supported_outfmt": "5" if outfmt == 5 else "6 std",
        "outfmt": outfmt,
        "format": report_format,
        "parent_job_id": parent_job_id,
        "child_count": len(child_reports),
        "max_target_seqs": next(iter(max_target_values)) if len(max_target_values) == 1 else None,
        **totals,
        "warnings": warnings,
        "children": child_items,
    }


def _iter_split_child_merged_result_chunks(
    *,
    storage_account: str,
    children: list[Any],
    credential: Any,
) -> Iterator[bytes]:
    from api.services.storage_data import stream_blob_bytes

    for child in children:
        child_job_id = str(getattr(child, "job_id", "") or "")
        path = _split_child_result_paths(child_job_id)["merged_result_path"]
        yield from stream_blob_bytes(credential, storage_account, "results", path)


def _read_split_child_merged_result_bytes(
    *,
    storage_account: str,
    child: Any,
    credential: Any,
) -> bytes:
    from api.services.storage_data import stream_blob_bytes

    child_job_id = str(getattr(child, "job_id", "") or "")
    path = _split_child_result_paths(child_job_id)["merged_result_path"]
    return b"".join(stream_blob_bytes(credential, storage_account, "results", path))


def _build_parent_split_xml_result_bytes(
    *,
    storage_account: str,
    children: list[Any],
    credential: Any,
) -> bytes:
    """Build one valid BLAST XML gzip from disjoint child XML result files."""
    import copy
    import gzip
    import io
    import xml.etree.ElementTree as ET

    base_root: ET.Element | None = None
    base_iterations: ET.Element | None = None
    for child in children:
        raw_gzip = _read_split_child_merged_result_bytes(
            storage_account=storage_account,
            child=child,
            credential=credential,
        )
        try:
            xml_payload = gzip.decompress(raw_gzip)
            child_root = ET.fromstring(xml_payload)  # noqa: S314 -- backend-generated BLAST XML
        except (OSError, ET.ParseError) as exc:
            child_job_id = str(getattr(child, "job_id", "") or "")
            raise ValueError(f"invalid child XML result: {child_job_id}") from exc
        if child_root.tag != "BlastOutput":
            child_job_id = str(getattr(child, "job_id", "") or "")
            raise ValueError(f"unexpected child XML root for {child_job_id}: {child_root.tag}")
        child_iterations = child_root.find("BlastOutput_iterations")
        if child_iterations is None:
            child_job_id = str(getattr(child, "job_id", "") or "")
            raise ValueError(f"child XML result has no iterations: {child_job_id}")
        if base_root is None:
            base_root = copy.deepcopy(child_root)
            base_iterations = base_root.find("BlastOutput_iterations")
            if base_iterations is None:
                base_iterations = ET.SubElement(base_root, "BlastOutput_iterations")
            base_iterations.clear()
            db_node = base_root.find("BlastOutput_db")
            if db_node is not None:
                db_node.text = "merged split-query child results"
        for iteration in child_iterations.findall("Iteration"):
            iteration_copy = copy.deepcopy(iteration)
            iter_num = iteration_copy.find("Iteration_iter-num")
            if iter_num is None:
                iter_num = ET.Element("Iteration_iter-num")
                iteration_copy.insert(0, iter_num)
            iter_num.text = str(len(base_iterations) + 1)  # type: ignore[arg-type]
            base_iterations.append(iteration_copy)  # type: ignore[union-attr]
    if base_root is None or base_iterations is None:
        raise ValueError("no child XML results to merge")
    ET.indent(base_root, space="  ")
    buffer = io.BytesIO()
    ET.ElementTree(base_root).write(buffer, encoding="utf-8", xml_declaration=True)
    return gzip.compress(buffer.getvalue())


def _write_split_parent_result_artifacts(
    *,
    parent_job_id: str,
    storage_account: str,
    children: list[Any],
    artifact_status: Mapping[str, Any],
    credential: Any | None = None,
) -> dict[str, Any]:
    """Create parent result artifacts from disjoint split-query child outputs.

    Each child has already run the terminal-side sharded finalizer for its own
    query group. Because split query groups are disjoint, parent assembly is a
    gzip-member concatenation plus report aggregation, not another top-N rerank.
    """
    if credential is None:
        from api.services import get_credential

        credential = get_credential()
    from api.services.storage_data import upload_blob_bytes, upload_blob_text

    paths = _parent_split_result_paths(parent_job_id)
    child_reports = _load_split_child_merge_reports(
        storage_account=storage_account,
        children=children,
        credential=credential,
    )
    parent_report = _aggregate_split_merge_reports(
        parent_job_id=parent_job_id,
        child_reports=child_reports,
    )
    is_xml_result = parent_report.get("format") == "blast_xml"
    manifest = {
        "parent_job_id": parent_job_id,
        "created_at": _now_iso(),
        "assembly": "xml_iteration_concatenation" if is_xml_result else "gzip_member_concatenation",
        "children": artifact_status.get("children", []),
        "outputs": {
            "merged_result_path": paths["merged_result_path"],
            "merge_report_path": paths["merge_report_path"],
            "manifest_path": paths["manifest_path"],
        },
    }

    upload_blob_bytes(
        credential,
        storage_account,
        "results",
        paths["merged_result_path"],
        [
            _build_parent_split_xml_result_bytes(
                storage_account=storage_account,
                children=children,
                credential=credential,
            )
        ]
        if is_xml_result
        else _iter_split_child_merged_result_chunks(
            storage_account=storage_account,
            children=children,
            credential=credential,
        ),
        content_type="application/gzip",
    )
    upload_blob_text(
        credential,
        storage_account,
        "results",
        paths["merge_report_path"],
        json.dumps(parent_report, sort_keys=True) + "\n",
        content_type="application/json; charset=utf-8",
    )
    upload_blob_text(
        credential,
        storage_account,
        "results",
        paths["manifest_path"],
        json.dumps(manifest, sort_keys=True) + "\n",
        content_type="application/json; charset=utf-8",
    )
    return {
        "parent_job_id": parent_job_id,
        "paths": paths,
        "report": parent_report,
        "manifest": manifest,
    }


def _finalize_split_parent_results(
    *,
    parent_job_id: str,
    storage_account: str,
    expected_child_count: int | None = None,
    child_limit: int = 1000,
    repo: Any | None = None,
    update_parent: bool = True,
    credential: Any | None = None,
) -> dict[str, Any]:
    """Verify child finalizer artifacts and complete a split parent result."""
    if repo is None:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
    if credential is None:
        from api.services import get_credential

        credential = get_credential()

    parent = repo.get(parent_job_id) if hasattr(repo, "get") else None
    existing = _parent_split_result_artifacts_present(
        parent_job_id=parent_job_id,
        storage_account=storage_account,
        credential=credential,
    )
    if existing["all_present"]:
        if update_parent:
            _update_state(
                parent_job_id,
                "completed",
                status="completed",
                event="split_results_already_merged",
                outputs=existing["paths"],
            )
        return {
            "parent_job_id": parent_job_id,
            "status": "completed",
            "phase": "completed",
            "already_merged": True,
            "outputs": existing["paths"],
        }
    if parent is not None and str(getattr(parent, "status", "") or "") == "completed":
        raise ValueError("split parent is completed but parent result artifacts are incomplete")

    aggregation = _aggregate_split_child_states(
        parent_job_id=parent_job_id,
        expected_child_count=expected_child_count,
        child_limit=child_limit,
        repo=repo,
        update_parent=update_parent,
    )
    if not aggregation["ready_for_merge"]:
        return aggregation

    children = list(repo.list_children(parent_job_id, limit=child_limit))
    artifact_status = _verify_split_child_result_artifacts(
        parent_job_id=parent_job_id,
        storage_account=storage_account,
        children=children,
        credential=credential,
    )
    if not artifact_status["all_artifacts_present"]:
        if update_parent:
            _update_state(
                parent_job_id,
                "split_results_waiting_for_artifacts",
                status="running",
                event="split_result_artifacts_missing",
                missing_artifacts=artifact_status["missing_artifacts"],
                child_count=len(children),
            )
        return {
            "parent_job_id": parent_job_id,
            "status": "running",
            "phase": "split_results_waiting_for_artifacts",
            "ready_for_merge": False,
            "artifact_status": artifact_status,
        }

    if update_parent:
        _update_state(
            parent_job_id,
            "split_results_merging",
            status="running",
            event="split_results_merge_started",
            child_count=len(children),
        )
    written = _write_split_parent_result_artifacts(
        parent_job_id=parent_job_id,
        storage_account=storage_account,
        children=children,
        artifact_status=artifact_status,
        credential=credential,
    )
    if update_parent:
        _update_state(
            parent_job_id,
            "completed",
            status="completed",
            event="split_results_merged",
            child_count=len(children),
            outputs=written["paths"],
            report={
                "precision_level": written["report"].get("precision_level"),
                "queries": written["report"].get("queries"),
                "total_output_hits": written["report"].get("total_output_hits"),
                "warnings": written["report"].get("warnings"),
            },
        )
    return {
        "parent_job_id": parent_job_id,
        "status": "completed",
        "phase": "completed",
        "already_merged": False,
        "child_count": len(children),
        "outputs": written["paths"],
        "report": written["report"],
    }


ELASTIC_BLAST_CFG_FILE = "elastic-blast.ini"


def _elastic_blast_argv(
    command: str,
    job_id: str,
    *,
    cfg_file: str = ELASTIC_BLAST_CFG_FILE,
    force: bool = False,
) -> list[str]:
    del job_id, force
    argv = [
        "elastic-blast",
        command,
        "--cfg",
        cfg_file,
    ]
    return argv


def _last_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _result_error(result: Mapping[str, Any], payload: Mapping[str, Any] | None) -> str:
    if payload and payload.get("kind") == "error":
        return _snippet(payload.get("message"))
    return _snippet(result.get("stderr") or result.get("stdout") or "elastic-blast failed")


def _is_retryable_result(result: Mapping[str, Any], payload: Mapping[str, Any] | None) -> bool:
    category = str((payload or {}).get("category", ""))
    if category in RETRYABLE_ERROR_CATEGORIES:
        return True
    try:
        return int(result.get("exit_code", 1)) in RETRYABLE_EXIT_CODES
    except (TypeError, ValueError):
        return False


def _retry_after(payload: Mapping[str, Any] | None, default: int) -> int:
    raw = (payload or {}).get("retry_after_seconds")
    try:
        parsed = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, 300))


def _enqueue_artifact_finalizer(job_id: str, phase: str, status: str) -> None:
    if not _phase_is_terminal_for_artifacts(phase, status):
        return
    try:
        from api.services.job_artifacts import artifact_build_should_enqueue
        from api.tasks.blast_artifacts import finalize_job_artifacts

        if not artifact_build_should_enqueue(job_id, ["artifact_finalizer"]):
            return
        finalize_job_artifacts.apply_async(kwargs={"job_id": job_id})
    except Exception as exc:
        LOGGER.info("artifact finalizer enqueue skipped job_id=%s: %s", job_id, type(exc).__name__)


def _update_state(
    job_id: str,
    phase: str,
    status: str = "running",
    *,
    event: str | None = None,
    error_code: str | None = None,
    **details: Any,
) -> None:
    """Best-effort state + history update.

    State storage must never crash the task execution path, but failures are
    visible in worker logs. History receives event-shaped payloads while the
    current row only stores compact status/phase/error fields.
    """

    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        stored_error_code = error_code or ""
        merged_payload: dict[str, Any] | None = None
        try:
            state = repo.get(job_id)
            if (
                event is None
                and not details
                and state is not None
                and str(getattr(state, "status", "") or "") == status
                and str(getattr(state, "phase", "") or "") == phase
                and str(getattr(state, "error_code", "") or "") == stored_error_code
            ):
                _enqueue_artifact_finalizer(job_id, phase, status)
                return
            existing_payload = state.payload if state is not None else None
            merged_payload = _merge_progress_payload(
                existing_payload if isinstance(existing_payload, Mapping) else None,
                phase=phase,
                status=status,
                error_code=stored_error_code,
                details=details,
            )
        except Exception as exc:
            LOGGER.debug("blast progress payload merge skipped job_id=%s: %s", job_id, exc)
        update_kwargs: dict[str, Any] = {
            "status": status,
            "phase": phase,
            "error_code": stored_error_code,
        }
        if merged_payload is not None:
            update_kwargs["payload"] = merged_payload
        repo.update(job_id, **update_kwargs)
        repo.append_history(
            job_id,
            event or phase,
            {
                "phase": phase,
                "status": status,
                "error_code": stored_error_code,
                "updated_at": _now_iso(),
                **details,
            },
        )
        _enqueue_artifact_finalizer(job_id, phase, status)
    except Exception as exc:
        LOGGER.warning("blast state update failed job_id=%s phase=%s: %s", job_id, phase, exc)


def _external_reconcile_job_id(row: Any) -> str:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    for value in (
        payload.get("elastic_blast_job_id"),
        payload.get("k8s_job_id"),
        getattr(row, "elastic_blast_job_id", ""),
        getattr(row, "k8s_job_id", ""),
    ):
        job_id = str(value or "").strip()
        if job_id.startswith("job-"):
            return job_id
    return ""


def _reconcile_row_k8s_status(
    repo: Any,
    row: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    elastic_blast_job_id: str,
) -> str:
    if not (subscription_id and resource_group and cluster_name and elastic_blast_job_id):
        return ""
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        k8s = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=elastic_blast_job_id,
        )
    except Exception as exc:
        LOGGER.info(
            "reconcile_stale_jobs: k8s refresh skipped job_id=%s elastic_blast_job_id=%s: %s",
            row.job_id,
            elastic_blast_job_id,
            type(exc).__name__,
        )
        return ""

    k8s_status = str(k8s.get("status") or "")
    if k8s_status == "completed":
        if _has_parseable_result_artifact(_storage_account_from_row(row), str(row.job_id)):
            status, phase, outcome = "completed", "completed", "completed"
        else:
            status, phase, outcome = "running", "results_pending", "results_pending"
    elif k8s_status == "failed":
        status, phase, outcome = "failed", "failed", "failed"
    elif k8s_status == "running":
        status, phase, outcome = "running", "running", "running"
    elif k8s_status == "creating":
        status, phase, outcome = "running", "submitted", "running"
    else:
        return ""

    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else None
    merged_payload = _merge_progress_payload(
        payload,
        phase=phase,
        status=status,
        error_code="",
        details={"k8s": k8s, "source": "k8s_reconcile"},
    )
    repo.update(row.job_id, status=status, phase=phase, payload=merged_payload)
    if status in {"completed", "failed"}:
        _enqueue_artifact_finalizer(row.job_id, phase, status)
    return outcome


def _row_has_container_runtime_metrics(row: Any) -> bool:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    progress = payload.get("_progress") if isinstance(payload, Mapping) else None
    steps = progress.get("steps") if isinstance(progress, Mapping) else None
    running = steps.get("running") if isinstance(steps, Mapping) else None
    k8s = running.get("k8s") if isinstance(running, Mapping) else None
    return isinstance(k8s, Mapping) and any(
        k8s.get(key) not in (None, "")
        for key in ("blast_container_duration_ms", "results_export_container_duration_ms")
    )


def _completed_row_runtime_job_id(row: Any) -> str:
    root_job_id = _external_reconcile_job_id(row)
    if root_job_id:
        return root_job_id
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    progress = payload.get("_progress") if isinstance(payload, Mapping) else None
    steps = progress.get("steps") if isinstance(progress, Mapping) else None
    running = steps.get("running") if isinstance(steps, Mapping) else None
    k8s = running.get("k8s") if isinstance(running, Mapping) else None
    if isinstance(k8s, Mapping):
        runtime_job_id = str(k8s.get("job_id") or "").strip()
        if runtime_job_id.startswith("job-"):
            return runtime_job_id
    return _discover_elastic_blast_job_id(_storage_account_from_row(row), str(row.job_id))


def _completed_row_runtime_scope(row: Any) -> tuple[str, str, str, str]:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    subscription_id = str(
        payload.get("subscription_id") or getattr(row, "subscription_id", "") or ""
    )
    resource_group = str(
        payload.get("resource_group") or getattr(row, "resource_group", "") or ""
    )
    cluster_name = str(
        payload.get("cluster_name")
        or payload.get("aks_cluster_name")
        or getattr(row, "cluster_name", "")
        or ""
    )
    return subscription_id, resource_group, cluster_name, _completed_row_runtime_job_id(row)


def _backfill_completed_row_runtime_metrics(repo: Any, row: Any) -> str:
    if _row_has_container_runtime_metrics(row):
        return "skipped"
    subscription_id, resource_group, cluster_name, elastic_blast_job_id = (
        _completed_row_runtime_scope(row)
    )
    if not (subscription_id and resource_group and cluster_name and elastic_blast_job_id):
        return "skipped"
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        k8s = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=elastic_blast_job_id,
        )
    except Exception as exc:
        LOGGER.info(
            "completed runtime backfill skipped job_id=%s elastic_blast_job_id=%s: %s",
            row.job_id,
            elastic_blast_job_id,
            type(exc).__name__,
        )
        return "error"
    if str(k8s.get("status") or "") != "completed":
        return "skipped"
    if not any(
        k8s.get(key) not in (None, "")
        for key in ("blast_container_duration_ms", "results_export_container_duration_ms")
    ):
        return "skipped"
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else None
    merged_payload = _payload_with_backfilled_runtime_metrics(payload, k8s)
    repo.update(
        row.job_id,
        status="completed",
        phase="completed",
        payload=merged_payload,
        updated_at=getattr(row, "updated_at", None),
    )
    try:
        repo.append_history(
            row.job_id,
            "k8s_completed_runtime_backfilled",
            {"status": "completed", "phase": "completed", "k8s": k8s},
        )
    except Exception as exc:
        LOGGER.debug(
            "completed runtime backfill history skipped job_id=%s: %s",
            row.job_id,
            type(exc).__name__,
        )
    return "backfilled"


def _payload_with_backfilled_runtime_metrics(
    payload: Mapping[str, Any] | None,
    k8s: Mapping[str, Any],
) -> dict[str, Any]:
    out = deepcopy(dict(payload or {}))
    progress = out.get("_progress") if isinstance(out.get("_progress"), dict) else {}
    steps = progress.get("steps") if isinstance(progress.get("steps"), dict) else {}
    running = steps.get("running") if isinstance(steps.get("running"), dict) else {}
    existing_k8s = running.get("k8s") if isinstance(running.get("k8s"), dict) else {}
    running["k8s"] = {**existing_k8s, **dict(k8s)}
    running.setdefault("phase", "running")
    running.setdefault("status", "completed")
    if not running.get("started_at") and k8s.get("started_at"):
        running["started_at"] = k8s["started_at"]
    if not running.get("completed_at") and k8s.get("completed_at"):
        running["completed_at"] = k8s["completed_at"]
    if k8s.get("started_at") and k8s.get("completed_at"):
        running.setdefault("duration_source", "k8s_runtime")
    steps["running"] = running
    progress["steps"] = steps
    progress.setdefault("phase", "completed")
    progress.setdefault("status", "completed")
    out["_progress"] = progress
    return out


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
                _progress(task, progress_phase, **progress_kwargs)
                _update_state(
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
        from api.services.blast_result_analytics import list_parseable_result_blobs

        return bool(list_parseable_result_blobs(storage_account, job_id))
    except Exception as exc:
        LOGGER.info("result artifact check skipped job_id=%s: %s", job_id, type(exc).__name__)
        return False


def _extract_elastic_blast_job_id(output: object) -> str:
    text = str(output or "")
    match = ELASTIC_BLAST_JOB_ID_RE.search(text)
    return match.group(1) if match else ""


def _discover_elastic_blast_job_id(storage_account: str, job_id: str) -> str:
    if not storage_account or not job_id:
        return ""
    try:
        from api.services import get_credential
        from api.services.storage_data import _blob_service

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
        and not _has_parseable_result_artifact(
            storage_account,
            job_id,
        )
    ):
        return "results_pending", "running"
    return phase, status


def _progress(task: Any, phase: str, **details: Any) -> None:
    try:
        task.update_state(state="PROGRESS", meta={"phase": phase, **details})
    except Exception:
        LOGGER.debug("celery progress update failed", exc_info=True)


def _retry_or_fail(
    task: Any,
    *,
    job_id: str,
    phase: str,
    exc: BaseException,
    error_code: str,
    retry_after_seconds: int | None = None,
) -> dict[str, Any]:
    request = getattr(task, "request", None)
    retries = int(getattr(request, "retries", 0) or 0)
    max_retries = getattr(task, "max_retries", 0) or 0
    if retries >= max_retries:
        error = _snippet(exc)
        _update_state(job_id, phase, status="failed", error_code=error_code, error=error)
        return {"job_id": job_id, "status": "failed", "phase": phase, "error": error}

    countdown = retry_after_seconds or min(300, 15 * (2**retries))
    _update_state(
        job_id,
        phase,
        status="running",
        event="retry_scheduled",
        error_code=error_code,
        retry_after_seconds=countdown,
        attempt=retries + 1,
        error=_snippet(exc),
    )
    raise task.retry(exc=exc, countdown=countdown)


def _submit_success_status(payload: Mapping[str, Any] | None) -> tuple[str, str]:
    decision = str((payload or {}).get("decision", "accepted"))
    details = (payload or {}).get("details")
    terminal = details.get("terminal") if isinstance(details, dict) else None
    if decision == "already_done" and terminal == "SUCCESS":
        return "completed", "completed"
    if decision == "already_done" and terminal == "FAILURE":
        return "failed", "failed"
    return "submitted", "running"


def _storage_account_from_row(row: Any) -> str:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    return str(getattr(row, "storage_account", "") or payload.get("storage_account") or "")


def _celery_success_row_status(row: Any, result: Any) -> tuple[str, str]:
    if not isinstance(result, Mapping):
        return "completed", "completed"
    status = str(result.get("status") or "").lower()
    phase = str(result.get("phase") or status or "completed")
    if status == "running":
        return "running", phase or "submitted"
    if status == "failed":
        return "failed", phase or "failed"
    if status == "completed" and not _has_parseable_result_artifact(
        _storage_account_from_row(row),
        str(row.job_id),
    ):
        return "running", "results_pending"
    return "completed", phase or "completed"


@shared_task(
    name="api.tasks.blast.submit",
    bind=True,
    max_retries=12,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def submit(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    program: str,
    database: str,
    query_file: str,
    options: dict[str, Any] | None = None,
    caller_oid: str = "",
    caller_tenant_id: str = "",
) -> dict[str, Any]:
    """Submit a BLAST search via the terminal sidecar.

    Side effects: writes ``elastic-blast.ini`` in the terminal sidecar workdir,
    executes ``elastic-blast submit --cfg elastic-blast.ini``, and updates
    Table-backed job state.
    """

    _progress(self, "preparing")
    _update_state(job_id, "preparing")
    effective_options = _suppress_sharding_for_unsharded_database(
        storage_account=storage_account,
        database=database,
        options=options,
    )
    effective_options = _expand_strict_tie_order_candidate_pool(effective_options)

    from concurrent.futures import Future, ThreadPoolExecutor

    from api.services.terminal_exec import run as terminal_run

    db_name_for_warmup = extract_db_name(database)
    will_split_parent = _requires_split_parent_submission(effective_options)

    _progress(self, "warming_up", database=db_name_for_warmup)
    _update_state(job_id, "warming_up", database=db_name_for_warmup)

    # Run the ~8s K8s warmup poll alongside the small Azure-side prep work
    # (Azure CLI login warmup + best-effort oracle blob uploads). The warmup
    # result is required to finalise effective_options, but the prep tasks
    # are independent — fan them out so warming_up wall time is the cap.
    warmup_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="blast-submit-prep")
    warmup_ready: dict[str, Any] | None = None
    tie_order_oracle: dict[str, Any] | None = None
    db_order_oracle: dict[str, Any] | None = None
    try:
        warmup_future = warmup_pool.submit(
            _ensure_node_warmup_ready_for_submit,
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            database=database,
            storage_account=storage_account,
            options=effective_options,
        )
        az_login_future = warmup_pool.submit(_ensure_terminal_azure_cli_login, terminal_run)
        tie_oracle_future: Future[Any] | None = None
        db_oracle_future: Future[Any] | None = None
        if not will_split_parent:
            tie_oracle_future = warmup_pool.submit(
                upload_tie_order_oracle_if_present,
                storage_account=storage_account,
                job_id=job_id,
                options=effective_options,
            )
            db_oracle_future = warmup_pool.submit(
                upload_db_order_oracle_pointer_if_available,
                storage_account=storage_account,
                job_id=job_id,
                database=database,
                options=effective_options,
            )

        try:
            warmup_ready = warmup_future.result()
        except WarmupNotReadyError as exc:
            error = _snippet(exc)
            _update_state(
                job_id,
                "warmup_not_ready",
                status="failed",
                error_code="node_warmup_not_ready",
                last_output=error,
            )
            return {
                "job_id": job_id,
                "status": "failed",
                "phase": "warmup_not_ready",
                "error": error,
            }

        if warmup_ready is not None:
            effective_options = dict(effective_options or {})
            effective_options["skip_warmed_ssd_init"] = True
            _progress(
                self,
                "warmup_ready",
                database=db_name_for_warmup,
                warmup=warmup_ready,
            )
            _update_state(
                job_id,
                "warmup_ready",
                status="running",
                warmup=warmup_ready,
            )

        try:
            az_login_future.result()
        except TerminalAzureLoginError as exc:
            return _retry_or_fail(
                self,
                job_id=job_id,
                phase="terminal_az_login_failed",
                exc=exc,
                error_code="terminal_az_login_failed",
            )
        except TerminalExecError as exc:
            return _retry_or_fail(
                self,
                job_id=job_id,
                phase="terminal_unavailable",
                exc=exc,
                error_code="terminal_exec_unavailable",
            )

        if tie_oracle_future is not None:
            try:
                tie_order_oracle = tie_oracle_future.result()
            except Exception as exc:
                LOGGER.warning(
                    "tie_order_oracle upload failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                tie_order_oracle = None
        if db_oracle_future is not None:
            try:
                db_order_oracle = db_oracle_future.result()
            except Exception as exc:
                LOGGER.warning(
                    "db_order_oracle upload failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
                db_order_oracle = None
    finally:
        warmup_pool.shutdown(wait=False, cancel_futures=False)

    if will_split_parent:
        _progress(self, "splitting_queries")
        try:
            return _run_storage_query_split_parent_submission(
                parent_job_id=job_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                storage_account=storage_account,
                program=program,
                database=database,
                query_file=query_file,
                query_effective_search_spaces=(effective_options or {}).get(
                    "query_effective_search_spaces"
                ),
                options=effective_options,
                owner_oid=caller_oid,
                tenant_id=caller_tenant_id,
            )
        except ValueError as exc:
            error = _snippet(exc)
            _update_state(job_id, "split_submit_invalid", status="failed", error_code=error)
            return {
                "job_id": job_id,
                "status": "failed",
                "phase": "split_submit_invalid",
                "error": error,
            }
        except Exception as exc:
            return _retry_or_fail(
                self,
                job_id=job_id,
                phase="split_submit_unavailable",
                exc=exc,
                error_code="split_submit_unavailable",
            )

    if tie_order_oracle is not None:
        _progress(self, "tie_order_oracle_uploaded", tie_order_oracle=tie_order_oracle)
        _update_state(
            job_id,
            "tie_order_oracle_uploaded",
            status="running",
            tie_order_oracle=tie_order_oracle,
        )
    if db_order_oracle is not None:
        _progress(self, "db_order_oracle_attached", db_order_oracle=db_order_oracle)
        _update_state(
            job_id,
            "db_order_oracle_attached",
            status="running",
            db_order_oracle=db_order_oracle,
        )

    try:
        config_content = _build_config_content(
            job_id=job_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            storage_account=storage_account,
            program=program,
            database=database,
            query_file=query_file,
            options=effective_options,
        )
        config_blob_path = f"{job_id}/{ELASTIC_BLAST_CFG_FILE}"
        try:
            from api.services import get_credential
            from api.services.storage_data import upload_blob_text

            config_url = upload_blob_text(
                get_credential(),
                storage_account,
                "queries",
                config_blob_path,
                config_content,
            )
            _progress(
                self,
                "configuring",
                config_blob_path=f"queries/{config_blob_path}",
                config_url=config_url,
            )
            _update_state(
                job_id,
                "configuring",
                status="running",
                config_blob_path=f"queries/{config_blob_path}",
                config_url=config_url,
            )
        except Exception as exc:
            LOGGER.warning("config preview upload failed job_id=%s: %s", job_id, type(exc).__name__)
            _update_state(
                job_id,
                "configuring",
                status="running",
                config_blob_path=f"queries/{config_blob_path}",
                config_upload_error=type(exc).__name__,
            )
    except Exception as exc:  # configuration errors are caller/actionable, not retryable
        error = _snippet(exc)
        _update_state(job_id, "config_invalid", status="failed", error_code=error)
        return {"job_id": job_id, "status": "failed", "phase": "config_invalid", "error": error}

    requires_node_warmup = _submit_requires_node_warmup(effective_options)
    reuses_warmed_ssd = requires_node_warmup and bool(
        (effective_options or {}).get("skip_warmed_ssd_init")
    )
    if requires_node_warmup:
        if reuses_warmed_ssd:
            _progress(
                self,
                "staging_db",
                skipped=True,
                decision="warmed_ssd_reused",
            )
            _update_state(
                job_id,
                "staging_db",
                status="completed",
                skipped=True,
                decision="warmed_ssd_reused",
                skip_reason="node_local_ssd_warmup_ready",
                output="Node-local DB warmup is ready; ElasticBLAST SSD initialization is skipped.",
            )
            _progress(self, "submitting")
            _update_state(job_id, "submitting")
        else:
            _progress(self, "staging_db")
            _update_state(job_id, "staging_db")
    else:
        _progress(self, "submitting")
        _update_state(job_id, "submitting")

    try:
        lock_key = submit_lock_key(cluster_name, "default")
        submit_lock = acquire_submit_lock(job_id, lock_key=lock_key)
        if submit_lock is None:
            return _retry_or_fail(
                self,
                job_id=job_id,
                phase="waiting_for_submit_slot",
                exc=RuntimeError(
                    "another ElasticBLAST submit is configuring Kubernetes resources "
                    f"on cluster={cluster_name} namespace=default"
                ),
                error_code="blast_submit_lock_busy",
                retry_after_seconds=30,
            )
        lock_client, lock_token = submit_lock
        try:
            # Azure CLI login was warmed up alongside the warmup poll; retry
            # here only if the cached identity expired between then and now.
            _ensure_terminal_azure_cli_login(terminal_run)
            result = _stream_submit_command(
                job_id=job_id,
                task=self,
                config_content=config_content,
                progress_phase="submitting",
            )
        finally:
            release_submit_lock(lock_client, lock_token, lock_key=lock_key)
    except TerminalExecError as exc:
        return _retry_or_fail(
            self,
            job_id=job_id,
            phase="terminal_unavailable",
            exc=exc,
            error_code="terminal_exec_unavailable",
        )
    except TerminalAzureLoginError as exc:
        return _retry_or_fail(
            self,
            job_id=job_id,
            phase="terminal_az_login_failed",
            exc=exc,
            error_code="terminal_az_login_failed",
        )

    submit_log_events = result.pop("_log_events", [])
    if isinstance(submit_log_events, list):
        persist_submit_log_events(
            job_id=job_id,
            progress_phase="submitting",
            events=submit_log_events,
        )
    if result.get("stdout") or result.get("stderr"):
        _update_state(
            job_id,
            "submitting",
            status="running",
            event="submit_log",
            last_output=_tail_text(
                [str(line) for line in (result.get("stdout"), result.get("stderr")) if line]
            ),
            log_line_count=result.get("log_line_count"),
        )

    payload = _last_json(str(result.get("stdout", "")))
    exit_code = int(result.get("exit_code", 1) or 0)
    submit_output = "\n".join(
        str(value) for value in (result.get("stdout"), result.get("stderr")) if value
    )
    elastic_blast_job_id = _extract_elastic_blast_job_id(
        result.get("stdout")
    ) or _discover_elastic_blast_job_id(
        storage_account,
        job_id,
    )
    if exit_code == 0:
        if requires_node_warmup and not reuses_warmed_ssd:
            _update_state(
                job_id,
                "staging_db",
                status="completed",
                output=_snippet(submit_output, LIVE_OUTPUT_SNIPPET_CHARS),
                last_output=_snippet(submit_output, LIVE_OUTPUT_SNIPPET_CHARS),
                log_line_count=result.get("log_line_count"),
                exit_code=exit_code,
                terminal_duration_ms=result.get("duration_ms"),
                timed_out=result.get("timed_out"),
            )
        _update_state(
            job_id,
            "submitting",
            status="completed",
            output=_snippet(submit_output, LIVE_OUTPUT_SNIPPET_CHARS),
            last_output=_snippet(submit_output, LIVE_OUTPUT_SNIPPET_CHARS),
            log_line_count=result.get("log_line_count"),
            exit_code=exit_code,
            terminal_duration_ms=result.get("duration_ms"),
            timed_out=result.get("timed_out"),
        )
        phase, status = _submit_success_status(payload)
        if status == "running":
            phase, status, k8s_status = _refresh_submit_terminal_status(
                job_id=job_id,
                subscription_id=subscription_id,
                resource_group=resource_group,
                cluster_name=cluster_name,
                k8s_job_id=elastic_blast_job_id or None,
            )
        else:
            k8s_status = None
        phase, status = _gate_completed_submit_on_results(
            job_id=job_id,
            storage_account=storage_account,
            phase=phase,
            status=status,
        )
        _update_state(
            job_id,
            phase,
            status=status,
            decision=(payload or {}).get("decision"),
            cluster_name=(payload or {}).get("cluster_name"),
            elastic_blast_job_id=elastic_blast_job_id or None,
            k8s=k8s_status,
            output=_snippet(submit_output, STDOUT_SNIPPET_CHARS),
            exit_code=exit_code,
            elastic_blast_submit_duration_ms=result.get("duration_ms"),
            timed_out=result.get("timed_out"),
        )
        # Kick off the per-job poller so the dashboard catches the K8s →
        # completed transition within ~10 s instead of waiting up to 60 s
        # for the next beat reconcile tick. The poller self-throttles via
        # the shared K8s refresh interval and self-stops on terminal phases.
        if status == "running" and phase in _POLL_RUNNING_ELIGIBLE_PHASES:
            try:
                poll_running_status.apply_async(
                    kwargs={"job_id": job_id, "iteration": 0},
                    countdown=POLL_RUNNING_START_DELAY,
                    queue="blast",
                )
            except Exception as exc:
                LOGGER.warning(
                    "submit: poll_running_status enqueue failed job_id=%s: %s",
                    job_id,
                    type(exc).__name__,
                )
        return {
            "job_id": job_id,
            "status": status,
            "phase": phase,
            "decision": (payload or {}).get("decision", "accepted"),
            "k8s": k8s_status,
            "output": _snippet(submit_output, STDOUT_SNIPPET_CHARS),
        }

    error = _result_error(result, payload)
    if _is_retryable_result(result, payload):
        return _retry_or_fail(
            self,
            job_id=job_id,
            phase="submit_retryable_failure",
            exc=RuntimeError(error),
            error_code=str((payload or {}).get("category") or "submit_retryable_failure"),
            retry_after_seconds=_retry_after(payload, default=30),
        )

    _update_state(job_id, "submit_failed", status="failed", error_code=error)
    return {"job_id": job_id, "status": "failed", "phase": "submit_failed", "error": error}


@shared_task(name="api.tasks.blast.merge_split_results", bind=True, max_retries=3)
def merge_split_results(
    self: Any,
    *,
    parent_job_id: str,
    storage_account: str,
    expected_child_count: int | None = None,
) -> dict[str, Any]:
    """Finalize a split-query parent job after all child finalizers finish.

    Side effects: verifies child result artifacts in Storage, writes parent
    result artifacts in the results container, and marks the parent completed
    only after those writes succeed. Idempotent: retries return completed when
    parent artifacts already exist.
    """
    _progress(self, "split_results_merging", parent_job_id=parent_job_id)
    try:
        return _finalize_split_parent_results(
            parent_job_id=parent_job_id,
            storage_account=storage_account,
            expected_child_count=expected_child_count,
        )
    except ValueError as exc:
        error = _snippet(exc)
        _update_state(
            parent_job_id,
            "split_results_merge_invalid",
            status="failed",
            error_code=error,
        )
        return {
            "parent_job_id": parent_job_id,
            "status": "failed",
            "phase": "split_results_merge_invalid",
            "error": error,
        }
    except Exception as exc:
        return _retry_or_fail(
            self,
            job_id=parent_job_id,
            phase="split_results_merge_unavailable",
            exc=exc,
            error_code="split_results_merge_unavailable",
            retry_after_seconds=30,
        )


@shared_task(name="api.tasks.blast.cancel", bind=True, max_retries=2)
def cancel(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
) -> dict[str, Any]:
    """Cancel a BLAST job by deleting its labelled Kubernetes Jobs."""

    _progress(self, "cancelling")
    _update_state(job_id, "cancelling")

    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_cancel_blast_job
        from api.services.state_repo import JobStateRepository

        credential = get_credential()
        repo = JobStateRepository()
        children = list(repo.list_children(job_id, limit=1000))
        target_job_ids = [str(child.job_id) for child in children] or [job_id]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for target_job_id in target_job_ids:
            result = k8s_cancel_blast_job(
                credential,
                subscription_id,
                resource_group,
                cluster_name,
                namespace="default",
                job_id=target_job_id,
            )
            results.append({"job_id": target_job_id, **result})
            if result.get("status") in {"cancelled", "unknown"}:
                if target_job_id != job_id:
                    repo.update(target_job_id, status="cancelled", phase="cancelled")
                    repo.append_history(
                        target_job_id,
                        "cancelled_by_parent",
                        {"parent_job_id": job_id},
                    )
                continue
            errors.append({"job_id": target_job_id, "errors": result.get("errors")})
    except Exception as exc:
        return _retry_or_fail(
            self,
            job_id=job_id,
            phase="cancel_unavailable",
            exc=exc,
            error_code="cancel_unavailable",
        )

    if not errors:
        child_count = len(target_job_ids) if target_job_ids != [job_id] else 0
        _update_state(
            job_id,
            "cancelled",
            status="cancelled",
            k8s={"targets": results},
            child_count=child_count,
            storage_account=storage_account,
        )
        return {"job_id": job_id, "status": "cancelled", "k8s": {"targets": results}}

    error = _snippet(errors or "Kubernetes cancellation did not complete")
    return _retry_or_fail(
        self,
        job_id=job_id,
        phase="cancel_retryable_failure",
        exc=RuntimeError(error),
        error_code="cancel_retryable_failure",
        retry_after_seconds=30,
    )


@shared_task(name="api.tasks.blast.check_status", bind=True)
def check_status(
    self: Any,
    *,
    job_id: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
) -> dict[str, Any]:
    """Check the status of a running BLAST job via the direct K8s API."""

    del self
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        children = list(repo.list_children(job_id, limit=1000))
        if children:
            aggregation = _aggregate_split_child_states(
                parent_job_id=job_id,
                repo=repo,
                child_limit=1000,
            )
            if aggregation["ready_for_merge"]:
                return _finalize_split_parent_results(
                    parent_job_id=job_id,
                    storage_account=storage_account,
                    repo=repo,
                    child_limit=1000,
                )
            return aggregation
    except Exception as exc:
        LOGGER.info("split parent status aggregation skipped job_id=%s: %s", job_id, exc)

    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        result = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=job_id,
        )
    except Exception as exc:
        error = _snippet(exc)
        LOGGER.warning("blast status check failed job_id=%s: %s", job_id, error)
        _update_state(job_id, "status_unavailable", status="running", error_code=error)
        return {"job_id": job_id, "status": "unknown", "error": error}

    status = str(result.get("status", "unknown"))
    state_status = {
        "completed": "completed",
        "failed": "failed",
        "running": "running",
        "creating": "running",
    }.get(status, "running")
    _update_state(job_id, status, status=state_status, k8s=result)
    return {"job_id": job_id, "status": state_status, "phase": status, "k8s": result}


# Per-job poller cadence and cap.
#
# A submit task enqueues ``poll_running_status`` with countdown=POLL_RUNNING_START_DELAY,
# and each iteration that observes a still-active row self-reschedules with
# countdown=POLL_RUNNING_INTERVAL. The cap (POLL_RUNNING_MAX_ITERATIONS) bounds
# a single submit's poll chain to ~30 minutes so we never leave a runaway
# polling chain behind if something goes sideways. The 60 s beat reconcile is
# still the safety net for any row whose poll chain ended early.
POLL_RUNNING_START_DELAY = 10
POLL_RUNNING_INTERVAL = 10
POLL_RUNNING_MAX_ITERATIONS = 180
_POLL_RUNNING_ELIGIBLE_PHASES = frozenset({"submitted", "running", "results_pending"})


@shared_task(name="api.tasks.blast.poll_running_status", bind=True)
def poll_running_status(
    self: Any,
    *,
    job_id: str,
    iteration: int = 0,
) -> dict[str, Any]:
    """Per-job poller that closes the K8s → dashboard latency gap after submit.

    The ``submit`` task enqueues this with a short countdown so the dashboard
    flips a row to ``completed`` within ~10 s of the K8s job finishing, instead
    of waiting up to 60 s for the next beat tick of ``reconcile_stale_jobs``.
    This task is idempotent: it reads the current row, asks
    ``_refresh_running_blast_state`` to do one K8s check (subject to the same
    per-job throttle the detail/list endpoints use), and self-reschedules only
    while the row is still active.
    """
    del self
    summary: dict[str, Any] = {
        "job_id": job_id,
        "iteration": iteration,
        "status": "unknown",
        "phase": "unknown",
        "rescheduled": False,
    }

    try:
        from api.services.blast_job_state import (
            _K8S_REFRESH_PHASES,
            _refresh_running_blast_state,
        )
        from api.services.state_repo import JobStateRepository
    except Exception as exc:
        LOGGER.warning("poll_running_status: dependency unavailable: %s", exc)
        return {**summary, "error": type(exc).__name__}

    try:
        repo = JobStateRepository()
        row = repo.get(job_id)
    except Exception as exc:
        LOGGER.info("poll_running_status: state lookup failed job_id=%s: %s", job_id, exc)
        return {**summary, "error": type(exc).__name__}

    if row is None:
        return {**summary, "status": "missing"}

    current_status = str(getattr(row, "status", "") or "").strip().casefold()
    current_phase = str(getattr(row, "phase", "") or "").strip().casefold()
    summary["status"] = current_status
    summary["phase"] = current_phase

    if current_status not in {"running", "pending", "queued"}:
        return summary
    if current_phase not in _K8S_REFRESH_PHASES:
        return summary

    try:
        refreshed = _refresh_running_blast_state(repo, row)
    except Exception as exc:
        LOGGER.info(
            "poll_running_status: refresh failed job_id=%s iteration=%d: %s",
            job_id,
            iteration,
            type(exc).__name__,
        )
        refreshed = row

    refreshed_status = str(getattr(refreshed, "status", "") or "").strip().casefold()
    refreshed_phase = str(getattr(refreshed, "phase", "") or "").strip().casefold()
    summary["status"] = refreshed_status
    summary["phase"] = refreshed_phase

    if refreshed_status not in {"running", "pending", "queued"}:
        return summary
    if refreshed_phase not in _K8S_REFRESH_PHASES:
        return summary
    if iteration + 1 >= POLL_RUNNING_MAX_ITERATIONS:
        LOGGER.info(
            "poll_running_status: max iterations reached job_id=%s — beat reconcile takes over",
            job_id,
        )
        return summary

    try:
        poll_running_status.apply_async(
            kwargs={"job_id": job_id, "iteration": iteration + 1},
            countdown=POLL_RUNNING_INTERVAL,
            queue="blast",
        )
        summary["rescheduled"] = True
    except Exception as exc:
        LOGGER.warning(
            "poll_running_status: reschedule failed job_id=%s iteration=%d: %s",
            job_id,
            iteration,
            type(exc).__name__,
        )
    return summary


@shared_task(name="api.tasks.blast.reconcile_stale_jobs", bind=True)
def reconcile_stale_jobs(
    self: Any,
    *,
    stale_threshold_seconds: int = 600,
    limit: int = 200,
) -> dict[str, Any]:
    """Bring Table Storage back in sync when a worker died mid-flight.

    Scans all jobstate rows with an active status (``queued`` / ``pending``
    / ``running`` / ``reducing``) and refreshes them by:

     1. Asking Celery for the task result. ``FAILURE`` or revoked tasks
         become ``failed``; completed submit tasks continue into runtime
         reconciliation while terminal task results become ``completed``.
     2. Refreshing the Kubernetes runtime status for accepted ElasticBLAST
         jobs and waiting in ``results_pending`` until parseable result
         artifacts exist.
     3. Falling back to the external OpenAPI plane when Celery has no
         record (worker died, broker lost the message, etc.).
     4. Marking rows ``failed`` with ``error_code=worker_lost`` when no
       upstream still knows about the job and the row has been quiet for
       longer than ``stale_threshold_seconds``.

    Runs every minute via the beat schedule registered in
    ``api/celery_app.py``. Idempotent — calling it twice in a row is a
    no-op if the first pass already brought every row to a terminal
    state.
    """
    del self
    from datetime import datetime

    from celery.result import AsyncResult

    from api.celery_app import celery_app

    summary: dict[str, Any] = {
        "scanned": 0,
        "completed": 0,
        "failed": 0,
        "worker_lost": 0,
        "k8s_refreshed": 0,
        "results_pending": 0,
        "external_refreshed": 0,
        "untouched": 0,
        "errors": 0,
    }

    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
    except Exception as exc:
        LOGGER.warning("reconcile_stale_jobs: state repo unavailable: %s", exc)
        summary["errors"] = 1
        return summary

    try:
        active_rows = repo.list_active(job_type="blast", limit=limit)
    except Exception as exc:
        LOGGER.warning("reconcile_stale_jobs: list_active failed: %s", exc)
        summary["errors"] = 1
        return summary

    summary["scanned"] = len(active_rows)
    now = datetime.now(UTC)

    for row in active_rows:
        try:
            task_id = (row.task_id or "").strip()
            celery_status: str | None = None
            celery_result: Any = None
            if task_id:
                try:
                    async_result = AsyncResult(task_id, app=celery_app)
                    celery_status = str(async_result.status or "").upper()
                    if celery_status in {"SUCCESS", "FAILURE"}:
                        celery_result = async_result.result
                except Exception as exc:
                    LOGGER.debug(
                        "reconcile_stale_jobs: AsyncResult failed job_id=%s: %s",
                        row.job_id,
                        type(exc).__name__,
                    )

            submit_task_completed_active = False

            # 1) Celery reports a terminal state. A completed submit task can
            # still leave an active runtime job in AKS, so active rows continue
            # into the K8s/OpenAPI reconciliation path below.
            if celery_status == "SUCCESS":
                status, phase = _celery_success_row_status(row, celery_result)
                if status == "completed":
                    if row.status != status or row.phase != phase:
                        repo.update(row.job_id, status=status, phase=phase)
                    _enqueue_artifact_finalizer(row.job_id, phase, status)
                    summary["completed"] += 1
                    continue
                if row.status != status or row.phase != phase:
                    repo.update(row.job_id, status=status, phase=phase)
                submit_task_completed_active = True
            if celery_status in {"FAILURE", "REVOKED"}:
                err = _snippet(celery_result) if celery_result is not None else "task_failed"
                repo.update(
                    row.job_id,
                    status="failed",
                    phase="failed",
                    error_code=err[:120],
                )
                _enqueue_artifact_finalizer(row.job_id, "failed", "failed")
                summary["failed"] += 1
                continue

            # 2) External OpenAPI may know the latest status when the
            #    local worker died but the BLAST runtime in AKS is still
            #    making progress.
            payload = row.payload or {}
            sub = payload.get("subscription_id") or row.subscription_id or ""
            rg = payload.get("resource_group") or row.resource_group or ""
            cluster = (
                payload.get("cluster_name")
                or payload.get("aks_cluster_name")
                or row.cluster_name
                or ""
            )
            refreshed = False
            external_job_id = _external_reconcile_job_id(row)
            k8s_outcome = _reconcile_row_k8s_status(
                repo,
                row,
                subscription_id=str(sub),
                resource_group=str(rg),
                cluster_name=str(cluster),
                elastic_blast_job_id=external_job_id,
            )
            if k8s_outcome:
                summary["k8s_refreshed"] += 1
                if k8s_outcome == "completed":
                    summary["completed"] += 1
                elif k8s_outcome == "failed":
                    summary["failed"] += 1
                elif k8s_outcome == "results_pending":
                    summary["results_pending"] += 1
                else:
                    summary["untouched"] += 1
                continue
            if sub and rg and cluster and external_job_id:
                try:
                    from api.routes._blast_shared import (
                        _external_to_blast_job,
                        _openapi_client_kwargs_from_cluster,
                    )
                    from api.services import external_blast

                    kwargs = _openapi_client_kwargs_from_cluster(sub, rg, cluster)
                    if kwargs:
                        detail = external_blast.get_job(external_job_id, **kwargs)
                        converted = _external_to_blast_job(detail)
                        ext_status = str(converted.get("status") or "")
                        ext_phase = str(converted.get("phase") or ext_status)
                        if ext_status and (ext_status != row.status or ext_phase != row.phase):
                            repo.update(
                                row.job_id,
                                status=ext_status,
                                phase=ext_phase,
                            )
                            summary["external_refreshed"] += 1
                            refreshed = True
                            if ext_status in {"completed", "failed"}:
                                _enqueue_artifact_finalizer(row.job_id, ext_phase, ext_status)
                                # Counted under external_refreshed; do not
                                # double-count under completed/failed.
                                pass
                except Exception as exc:
                    LOGGER.warning(
                        "reconcile_stale_jobs: external refresh failed job_id=%s "
                        "subscription_id=%s resource_group=%s cluster=%s error_type=%s "
                        "status_code=%s detail=%s",
                        row.job_id,
                        sub,
                        rg,
                        cluster,
                        type(exc).__name__,
                        getattr(exc, "status_code", ""),
                        _exception_detail_snippet(exc),
                    )
            if refreshed:
                continue

            if submit_task_completed_active:
                summary["untouched"] += 1
                continue

            # 3) Nobody knows the job and it has been quiet for a while.
            try:
                updated_at = datetime.fromisoformat(
                    (row.updated_at or row.created_at or "").replace("Z", "+00:00")
                )
            except Exception:
                updated_at = now  # never mark recently-created rows lost
            quiet_seconds = (now - updated_at).total_seconds()
            if quiet_seconds >= stale_threshold_seconds:
                repo.update(
                    row.job_id,
                    status="failed",
                    phase="worker_lost",
                    error_code="worker_lost",
                )
                _enqueue_artifact_finalizer(row.job_id, "worker_lost", "failed")
                summary["worker_lost"] += 1
            else:
                summary["untouched"] += 1
        except Exception as exc:
            LOGGER.warning(
                "reconcile_stale_jobs: row failed job_id=%s: %s",
                row.job_id,
                type(exc).__name__,
            )
            summary["errors"] += 1

    progress_made = (
        summary["completed"]
        or summary["failed"]
        or summary["worker_lost"]
        or summary["k8s_refreshed"]
        or summary["external_refreshed"]
    )
    if progress_made:
        LOGGER.info(
            "reconcile_stale_jobs: scanned=%(scanned)d completed=%(completed)d "
            "failed=%(failed)d worker_lost=%(worker_lost)d k8s_refreshed=%(k8s_refreshed)d "
            "results_pending=%(results_pending)d external_refreshed=%(external_refreshed)d "
            "errors=%(errors)d",
            summary,
        )
    return summary


@shared_task(name="api.tasks.blast.backfill_completed_runtime_metrics", bind=True)
def backfill_completed_runtime_metrics(
    self: Any,
    *,
    job_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Backfill K8s container runtime metrics for completed BLAST jobs.

    Side effects: updates completed dashboard job payloads when the K8s job
    still exposes container termination timestamps. Idempotent: rows that
    already carry container runtime metrics are skipped before any K8s call.
    """
    del self
    summary: dict[str, Any] = {
        "scanned": 0,
        "backfilled": 0,
        "skipped": 0,
        "errors": 0,
    }
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        if job_id:
            row = repo.get(job_id)
            rows = [row] if row is not None and row.status == "completed" else []
        else:
            rows = repo.list_completed(job_type="blast", limit=limit)
    except Exception as exc:
        LOGGER.warning("backfill_completed_runtime_metrics: list failed: %s", exc)
        summary["errors"] = 1
        return summary

    summary["scanned"] = len(rows)
    for row in rows:
        try:
            outcome = _backfill_completed_row_runtime_metrics(repo, row)
            if outcome == "backfilled":
                summary["backfilled"] += 1
            elif outcome == "error":
                summary["errors"] += 1
            else:
                summary["skipped"] += 1
        except Exception as exc:
            LOGGER.warning(
                "backfill_completed_runtime_metrics: row failed job_id=%s: %s",
                row.job_id,
                type(exc).__name__,
            )
            summary["errors"] += 1
    if summary["backfilled"] or summary["errors"]:
        LOGGER.info(
            "backfill_completed_runtime_metrics: scanned=%(scanned)d "
            "backfilled=%(backfilled)d skipped=%(skipped)d errors=%(errors)d",
            summary,
        )
    return summary
