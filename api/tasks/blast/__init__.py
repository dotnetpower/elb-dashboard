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

import logging
from collections.abc import Mapping
from datetime import UTC
from typing import Any

from api.services.blast import task_config as _blast_task_config
from api.services.blast.db_metadata import (
    resolve_db_metadata,  # noqa: F401  # re-exported for config_shims + tests
)
from api.services.blast.task_config import (
    WarmupNotReadyError,  # noqa: F401  # re-exported for tests
)
from api.services.query_grouping import (
    QuerySplitExecutionPlan,  # noqa: F401  # re-exported for split_pipeline + tests
)
from api.tasks.blast.progress import (
    _merge_progress_payload,  # noqa: F401  # re-exported for tests
    _phase_is_terminal_for_artifacts,  # noqa: F401  # re-exported for state + tests
    _tail_text,  # noqa: F401  # re-exported for submit_task post-stream snapshot
)
from api.tasks.blast.split_constants import (  # noqa: F401  # re-exported for split_pipeline + tests
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
    "cancel",
    "detect_submit_substep",
    "persist_submit_log_events",
    "release_submit_lock",
    "submit",
    "submit_lock_key",
)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _snippet(value: object, limit: int = 500) -> str:
    return _blast_task_config.snippet(value, limit)


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


def _storage_account_from_row(row: Any) -> str:
    payload = row.payload if isinstance(getattr(row, "payload", None), Mapping) else {}
    return str(getattr(row, "storage_account", "") or payload.get("storage_account") or "")



# Re-import task entry points defined in dedicated submodules so Celery's
# ``include=["api.tasks.blast"]`` discovery still picks them up and external
# callers (``from api.tasks.blast import cancel``) continue to work.
from api.tasks.blast.backfill_task import backfill_completed_runtime_metrics  # noqa: E402,F401
from api.tasks.blast.cancel_task import cancel  # noqa: E402
from api.tasks.blast.cli_parsing import (  # noqa: E402,F401
    ELASTIC_BLAST_CFG_FILE,
    ELASTIC_BLAST_JOB_ID_RE,
    INSUFFICIENT_MEMORY_GUIDANCE,
    MEMORY_LIMIT_GUIDANCE,
    RETRYABLE_ERROR_CATEGORIES,
    RETRYABLE_EXIT_CODES,
    _elastic_blast_argv,
    _extract_elastic_blast_job_id,
    _is_retryable_result,
    _last_json,
    _result_error,
    _retry_after,
    _submit_failure_guidance,
    _submit_success_status,
)
from api.tasks.blast.config_shims import (  # noqa: E402,F401
    BlastDatabaseAvailabilityError,
    _build_config_content,
    _disable_sharding_options,
    _ensure_node_warmup_ready_for_submit,
    _expand_strict_tie_order_candidate_pool,
    _metadata_has_prepared_shard_layout,
    _normalise_database_url,
    _normalise_query_url,
    _option_enabled,
    _query_blob_path_from_query_file,
    _relative_blob_path,
    _results_job_url,
    _storage_url,
    _submit_requires_node_warmup,
    _suppress_sharding_for_unsharded_database,
    _validate_blast_database_available,
    _validate_blast_database_ready,
)
from api.tasks.blast.poll_tasks import (  # noqa: E402,F401
    _POLL_RUNNING_ELIGIBLE_PHASES,
    POLL_RUNNING_INTERVAL,
    POLL_RUNNING_MAX_ITERATIONS,
    POLL_RUNNING_START_DELAY,
    check_status,
    poll_running_status,
)
from api.tasks.blast.reconcile_task import reconcile_stale_jobs  # noqa: E402,F401
from api.tasks.blast.split_pipeline import (  # noqa: E402,F401
    _aggregate_split_child_states,
    _aggregate_split_merge_reports,
    _build_parent_split_xml_result_bytes,
    _build_split_child_submit_plan,
    _child_state_payload,
    _dispatch_split_child_submits,
    _finalize_split_parent_results,
    _iter_parent_split_xml_chunks,
    _iter_split_child_merged_result_chunks,
    _load_split_child_merge_reports,
    _parent_split_result_artifacts_present,
    _parent_split_result_paths,
    _read_split_child_merged_result_bytes,
    _requires_split_parent_submission,
    _result_blob_map,
    _run_split_parent_submission,
    _run_storage_query_split_parent_submission,
    _split_child_options,
    _split_child_result_paths,
    _split_child_state_summary,
    _upload_split_query_files,
    _verify_split_child_result_artifacts,
    _write_split_parent_result_artifacts,
    merge_split_results,
)
from api.tasks.blast.state import (  # noqa: E402,F401
    _enqueue_artifact_finalizer,
    _progress,
    _retry_or_fail,
    _update_state,
)
from api.tasks.blast.submit_runtime import (  # noqa: E402,F401
    ERROR_SNIPPET_CHARS,
    LIVE_OUTPUT_SNIPPET_CHARS,
    STDOUT_SNIPPET_CHARS,
    SUBMIT_LIVE_STATE_UPDATE_INTERVAL_SECONDS,
    TerminalAzureLoginError,
    TerminalKubeconfigError,
    _discover_elastic_blast_job_id,
    _ensure_terminal_azure_cli_login,
    _ensure_terminal_kubeconfig_context,
    _exception_detail_snippet,
    _gate_completed_submit_on_results,
    _has_blast_success_marker,
    _has_parseable_result_artifact,
    _refresh_submit_terminal_status,
    _stream_submit_command,
    _strip_optional_unrecognized_params,
)
from api.tasks.blast.submit_task import submit  # noqa: E402


