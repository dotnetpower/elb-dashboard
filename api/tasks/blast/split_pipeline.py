"""Split-mode query pipeline helpers and the merge Celery task.

Responsibility: Plan, dispatch, and finalize split-mode BLAST submissions —
upload child FASTA shards, fan out per-shard submits, aggregate child state,
verify result artifacts in Storage, and merge them into the parent result
blobs that the dashboard surfaces. The merge Celery task drives the final
parent-side finalization step.
Edit boundaries: Everything here is split-mode specific. Shared helpers
(snippets, state updates, config builders, elastic_blast argv, etc.) stay in
``api.tasks.blast`` and are reached through ``_blast.X`` so monkeypatch tests
on the package keep working. Storage URL helpers stay in ``api.tasks.blast``
for the same reason.
Key entry points:
  - ``_blast._run_split_parent_submission`` /
    ``_blast._run_storage_query_split_parent_submission`` (called by ``submit``).
  - ``_blast._finalize_split_parent_results`` (called by ``check_status`` and the
    ``merge_split_results`` task).
  - ``merge_split_results`` (``@shared_task``
    ``name="api.tasks.blast.merge_split_results"``).
Risky contracts: Public task name must stay
``api.tasks.blast.merge_split_results``. Several helper names
(``_blast._finalize_split_parent_results``, ``_blast._run_split_parent_submission``,
``_blast._aggregate_split_child_states``, ``_blast._build_split_child_submit_plan``,
``_blast._dispatch_split_child_submits``, ``_blast._verify_split_child_result_artifacts``,
``_blast._write_split_parent_result_artifacts``, ``_blast._parent_split_result_paths``,
``_blast._requires_split_parent_submission``, ``_blast._upload_split_query_files``,
``_blast._run_storage_query_split_parent_submission``) are re-exported from
``__init__.py`` so tests can patch them via ``blast._X``.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from celery import shared_task

from api.services.query_grouping import QuerySplitExecutionPlan
from api.tasks import blast as _blast
from api.tasks.blast.split_constants import (
    SPLIT_CHILD_CANCELLED_STATUSES,
    SPLIT_CHILD_KNOWN_STATUSES,
    SPLIT_CHILD_MERGE_REPORT_BLOB,
    SPLIT_CHILD_MERGED_RESULT_BLOB,
    SPLIT_CHILD_OPTION_ALLOWLIST,
    SPLIT_MERGE_REPORT_MAX_BYTES,
    SPLIT_PARENT_MANIFEST_BLOB,
    SPLIT_UPLOAD_VERIFY_BYTES,
)

LOGGER = logging.getLogger(__name__)

__all__ = (
    "_aggregate_split_child_states",
    "_aggregate_split_merge_reports",
    "_build_parent_split_xml_result_bytes",
    "_build_split_child_submit_plan",
    "_child_state_payload",
    "_dispatch_split_child_submits",
    "_finalize_split_parent_results",
    "_iter_split_child_merged_result_chunks",
    "_load_split_child_merge_reports",
    "_parent_split_result_artifacts_present",
    "_parent_split_result_paths",
    "_read_split_child_merged_result_bytes",
    "_requires_split_parent_submission",
    "_result_blob_map",
    "_run_split_parent_submission",
    "_run_storage_query_split_parent_submission",
    "_split_child_options",
    "_split_child_result_paths",
    "_split_child_state_summary",
    "_upload_split_query_files",
    "_verify_split_child_result_artifacts",
    "_write_split_parent_result_artifacts",
    "merge_split_results",
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

        child_options = _blast._split_child_options(options)
        config_content = _blast._build_config_content(
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
                "argv": _blast._elastic_blast_argv("submit", child_job_id),
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

        payload = _blast._child_state_payload(child)
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
            stdin_file=_blast.ELASTIC_BLAST_CFG_FILE,
            timeout_seconds=600,
        )
        payload_json = _blast._last_json(str(result.get("stdout", "")))
        exit_code = int(result.get("exit_code", 1) or 0)
        if exit_code == 0:
            phase, status = _blast._submit_success_status(payload_json)
            repo.update(child_job_id, status=status, phase=phase)
            repo.append_history(
                child_job_id,
                phase,
                {
                    "parent_job_id": parent_job_id,
                    "group_id": child.get("group_id"),
                    "decision": (payload_json or {}).get("decision"),
                    "output": _blast._snippet(result.get("stdout"), _blast.STDOUT_SNIPPET_CHARS),
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

        error = _blast._result_error(result, payload_json)
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

    _blast._update_state(parent_job_id, "splitting_queries", event="split_queries_started")
    metadata = parse_fasta_metadata(query_fasta_text)
    split_plan = build_query_split_execution_plan(
        parent_job_id=parent_job_id,
        metadata=metadata,
        query_effective_search_spaces_value=query_effective_search_spaces,
        base_options=options,
    )
    if not split_plan.requires_split:
        raise ValueError("split parent submission requires mixed query effective search spaces")

    uploaded_groups = _blast._upload_split_query_files(
        storage_account=storage_account,
        plan=split_plan,
    )
    children = _blast._build_split_child_submit_plan(
        resource_group=resource_group,
        cluster_name=cluster_name,
        storage_account=storage_account,
        program=program,
        database=database,
        uploaded_groups=uploaded_groups,
    )
    dispatched = _blast._dispatch_split_child_submits(
        parent_job_id=parent_job_id,
        owner_oid=owner_oid,
        tenant_id=tenant_id,
        children=children,
        terminal_run=terminal_run,
    )
    failed = [child for child in dispatched if child.get("status") == "failed"]
    parent_phase = "split_children_failed" if failed else "split_children_submitted"
    parent_status = "failed" if failed else "running"
    _blast._update_state(
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

    query_blob_path = _blast._query_blob_path_from_query_file(
        storage_account=storage_account,
        query_file=query_file,
    )
    _blast._update_state(
        parent_job_id,
        "reading_split_query",
        event="split_query_read_started",
        query_file=query_blob_path,
        max_bytes=_blast.QUERY_FASTA_READ_MAX_BYTES,
    )

    query_fasta_text: str | None = None
    try:
        try:
            query_fasta_text = read_blob_text(
                get_credential(),
                storage_account,
                "queries",
                query_blob_path,
                max_bytes=_blast.QUERY_FASTA_READ_MAX_BYTES + 1,
            )
        except ResourceNotFoundError as exc:
            raise ValueError(
                f"query_file not found in queries container: {query_blob_path}"
            ) from exc

        if len(query_fasta_text.encode("utf-8")) > _blast.QUERY_FASTA_READ_MAX_BYTES:
            raise ValueError("query_file is too large for split planning")
        if not query_fasta_text.strip().startswith(">"):
            raise ValueError("query_file does not appear to be FASTA format")
    except Exception as exc:
        _blast._update_state(
            parent_job_id,
            "split_query_invalid",
            status="failed",
            error_code=_blast._snippet(exc),
            query_file=query_blob_path,
        )
        query_fasta_text = None
        raise

    try:
        return _blast._run_split_parent_submission(
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
        summaries.append(_blast._split_child_state_summary(child))

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
        _blast._update_state(
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
    child_id = _blast._relative_blob_path(child_job_id, "child_job_id")
    return {
        "merged_result_path": f"{child_id}/{SPLIT_CHILD_MERGED_RESULT_BLOB}",
        "merge_report_path": f"{child_id}/{SPLIT_CHILD_MERGE_REPORT_BLOB}",
    }


def _parent_split_result_paths(parent_job_id: str) -> dict[str, str]:
    parent_id = _blast._relative_blob_path(parent_job_id, "parent_job_id")
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
    paths = _blast._parent_split_result_paths(parent_job_id)
    blobs = _blast._result_blob_map(
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

        paths = _blast._split_child_result_paths(child_job_id)
        blobs = _blast._result_blob_map(
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
        path = _blast._split_child_result_paths(child_job_id)["merge_report_path"]
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
        path = _blast._split_child_result_paths(child_job_id)["merged_result_path"]
        yield from stream_blob_bytes(credential, storage_account, "results", path)


def _read_split_child_merged_result_bytes(
    *,
    storage_account: str,
    child: Any,
    credential: Any,
) -> bytes:
    from api.services.storage_data import stream_blob_bytes

    child_job_id = str(getattr(child, "job_id", "") or "")
    path = _blast._split_child_result_paths(child_job_id)["merged_result_path"]
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
        raw_gzip = _blast._read_split_child_merged_result_bytes(
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

    paths = _blast._parent_split_result_paths(parent_job_id)
    child_reports = _blast._load_split_child_merge_reports(
        storage_account=storage_account,
        children=children,
        credential=credential,
    )
    parent_report = _blast._aggregate_split_merge_reports(
        parent_job_id=parent_job_id,
        child_reports=child_reports,
    )
    is_xml_result = parent_report.get("format") == "blast_xml"
    manifest = {
        "parent_job_id": parent_job_id,
        "created_at": _blast._now_iso(),
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
            _blast._build_parent_split_xml_result_bytes(
                storage_account=storage_account,
                children=children,
                credential=credential,
            )
        ]
        if is_xml_result
        else _blast._iter_split_child_merged_result_chunks(
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
    existing = _blast._parent_split_result_artifacts_present(
        parent_job_id=parent_job_id,
        storage_account=storage_account,
        credential=credential,
    )
    if existing["all_present"]:
        if update_parent:
            _blast._update_state(
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

    aggregation = _blast._aggregate_split_child_states(
        parent_job_id=parent_job_id,
        expected_child_count=expected_child_count,
        child_limit=child_limit,
        repo=repo,
        update_parent=update_parent,
    )
    if not aggregation["ready_for_merge"]:
        return aggregation

    children = list(repo.list_children(parent_job_id, limit=child_limit))
    artifact_status = _blast._verify_split_child_result_artifacts(
        parent_job_id=parent_job_id,
        storage_account=storage_account,
        children=children,
        credential=credential,
    )
    if not artifact_status["all_artifacts_present"]:
        if update_parent:
            _blast._update_state(
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
        _blast._update_state(
            parent_job_id,
            "split_results_merging",
            status="running",
            event="split_results_merge_started",
            child_count=len(children),
        )
    written = _blast._write_split_parent_result_artifacts(
        parent_job_id=parent_job_id,
        storage_account=storage_account,
        children=children,
        artifact_status=artifact_status,
        credential=credential,
    )
    if update_parent:
        _blast._update_state(
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
    _blast._progress(self, "split_results_merging", parent_job_id=parent_job_id)
    try:
        return _blast._finalize_split_parent_results(
            parent_job_id=parent_job_id,
            storage_account=storage_account,
            expected_child_count=expected_child_count,
        )
    except ValueError as exc:
        error = _blast._snippet(exc)
        _blast._update_state(
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
        return _blast._retry_or_fail(
            self,
            job_id=parent_job_id,
            phase="split_results_merge_unavailable",
            exc=exc,
            error_code="split_results_merge_unavailable",
            retry_after_seconds=30,
        )

