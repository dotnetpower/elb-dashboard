"""External OpenAPI BLAST job -> dashboard projection helpers.

Pure transformation layer that turns a sibling-OpenAPI job dict into the
dashboard's BLAST job response shape. Extracted from
`api/services/blast/external_jobs.py` so the external-job *cache + table sync*
concern and the *projection* concern each own a single-responsibility module.

Responsibility: Map an external OpenAPI job dict into the dashboard job shape
    (status normalisation, error-code/message clamping, execution-shard summary,
    result-file list, database-metadata enrichment).
Edit boundaries: Pure-ish projection only — NO cache reads/writes, NO upstream
    OpenAPI client calls. May read Storage-backed display metadata via
    `db_metadata` (best-effort, never raises). The cache + sync lifecycle stays
    in `external_jobs.py`, which imports these helpers one-directionally.
Key entry points: `_external_to_blast_job`, `_external_status_to_dashboard`,
    `_external_error_message`, `_external_result_files`, `_short_external_db_name`.
Risky contracts: `error_code` MUST stay a short single token (reject whitespace
    / >80 chars) and the message MUST be whitespace-collapsed + 2000-char capped
    so an elastic-blast dump cannot bloat the Table row. `_external_to_blast_job`
    MUST NOT import the cache/sync layer (keep the dependency one-directional).
Validation: `uv run pytest -q api/tests/test_external_blast_api.py
    api/tests/test_blast_results_parser.py`.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

# A real error code is a short single token (no spaces/newlines), e.g.
# ``database_not_found`` / ``ImagePullBackOff`` / ``worker_lost``.
_MAX_ERROR_CODE_LEN = 80
# Stored error messages are capped so an elastic-blast dump (which can embed a
# full REDACTED HTTP header block) cannot bloat the Table row. The SPA clamps
# further for display; this is the storage-side ceiling.
_MAX_ERROR_MESSAGE_LEN = 2000

__all__ = [
    "_MAX_ERROR_CODE_LEN",
    "_MAX_ERROR_MESSAGE_LEN",
    "_clamp_error_message",
    "_database_metadata_for_response",
    "_external_error_message",
    "_external_execution_summary",
    "_external_result_files",
    "_external_status_to_dashboard",
    "_external_to_blast_job",
    "_normalise_error_code",
    "_short_external_db_name",
]


def _external_status_to_dashboard(status: str) -> str:
    if status in {"success", "completed"}:
        return "completed"
    if status in {"queued", "running", "failed", "cancelled"}:
        return status
    return "running" if status else "unknown"


def _short_external_db_name(*values: Any) -> str:
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        if raw.startswith(("http://", "https://", "az://")):
            parsed = urlparse(
                "https://" + raw.removeprefix("az://") if raw.startswith("az://") else raw
            )
            parts = [part for part in parsed.path.split("/") if part]
            if parts:
                return parts[-1]
        parts = [part for part in raw.replace("\\", "/").split("/") if part]
        return parts[-1] if parts else raw
    return ""


def _external_error_message(error: Any) -> tuple[str | None, str | None]:
    """Split an external job's ``error`` into ``(error_code, error_message)``.

    ``error_code`` is meant to be a short, greppable identifier (e.g.
    ``database_not_found``), never a full multi-line error body. elastic-blast
    failures arrive as a free-form string (or a dict whose ``code`` is actually
    the whole error text including a REDACTED Azure ``x-ms-*`` header dump), so
    we guard: a "code" candidate is only accepted when it is a single short
    token. Anything else is treated as the message. The message itself is
    newline-collapsed and length-capped so a 700+ char dump cannot bloat the
    Table row or the jobs-list response.
    """
    if not error:
        return None, None
    if isinstance(error, dict):
        raw_code = str(error.get("code") or "").strip()
        raw_message = str(error.get("message") or "").strip()
        code = _normalise_error_code(raw_code)
        # When the dict's "code" was actually a long body (not a real code),
        # fall back to it as the message so the detail is not lost.
        message_source = raw_message or (raw_code if code is None else "")
        message = _clamp_error_message(message_source) or (
            _clamp_error_message(raw_code) if raw_code else None
        )
        return code, message
    return None, _clamp_error_message(str(error))


def _normalise_error_code(raw: str) -> str | None:
    """Return ``raw`` only when it looks like a short single-token code."""
    token = raw.strip()
    if not token:
        return None
    if len(token) > _MAX_ERROR_CODE_LEN or any(c.isspace() for c in token):
        return None
    return token


def _clamp_error_message(raw: str) -> str | None:
    """Collapse whitespace and cap the length of a free-form error message."""
    collapsed = " ".join(str(raw).split())
    if not collapsed:
        return None
    if len(collapsed) > _MAX_ERROR_MESSAGE_LEN:
        return collapsed[: _MAX_ERROR_MESSAGE_LEN - 1].rstrip() + "\u2026"
    return collapsed


def _external_execution_summary(job: dict[str, Any]) -> dict[str, int]:
    execution = job.get("execution")
    if not isinstance(execution, dict):
        result = job.get("result")
        if isinstance(result, dict) and isinstance(result.get("execution"), dict):
            execution = result.get("execution")
    if not isinstance(execution, dict):
        return {}

    def number(key: str) -> int:
        value = execution.get(key)
        try:
            return max(0, int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    shard_count = number("shard_count")
    succeeded = number("shards_succeeded")
    active = number("shards_active")
    failed = number("shards_failed")
    done = min(shard_count, succeeded + failed) if shard_count else succeeded + failed
    out: dict[str, int] = {
        "splits_done": done,
        "splits_failed": failed,
    }
    if shard_count:
        out["splits_total"] = shard_count
    out["splits_active"] = active
    return out


def _external_to_blast_job(
    job: dict[str, Any],
    *,
    include_database_metadata: bool = False,
) -> dict[str, Any]:
    from api.services.response_contracts import build_target
    from api.services.state_repo import canonical_job_metadata

    external_status = str(job.get("status") or "unknown")
    status = _external_status_to_dashboard(external_status)
    metadata = canonical_job_metadata(
        {
            "job_title": job.get("job_title") or job.get("title"),
            "program": job.get("program"),
            "db": job.get("db_name") or job.get("db"),
            "query_file": job.get("query_file") or job.get("query"),
            "subscription_id": job.get("subscription_id"),
            "resource_group": job.get("resource_group"),
            "cluster_name": job.get("cluster_name"),
            "storage_account": job.get("storage_account"),
        },
        job_id=str(job.get("job_id") or ""),
    )
    db = metadata["db"]
    program = metadata["program"]
    created_at = str(job.get("created_at") or "")
    updated_at = str(
        job.get("updated_at")
        or job.get("last_progress_at")
        or job.get("completed_at")
        or job.get("failed_at")
        or created_at
    )
    source = str(job.get("submission_source") or "external_api")
    openapi_job_id = str(job.get("job_id") or "")
    dashboard_job_id = str(job.get("external_correlation_id") or "")
    error_code, error_message = _external_error_message(job.get("error"))
    out: dict[str, Any] = {
        "job_id": openapi_job_id,
        "job_id_kind": "openapi",
        "dashboard_job_id": dashboard_job_id or None,
        "openapi_job_id": openapi_job_id or None,
        "job_title": metadata["job_title"],
        "program": program,
        "db": db,
        "status": status,
        "phase": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "source": source,
        "submission_source": source,
        "external_correlation_id": job.get("external_correlation_id") or "",
        "query_label": metadata["query_label"] or "query.fa",
        "owner_upn": "api",
        "custom_status": {
            "phase": status,
            "blast_status": external_status,
            "progress_pct": job.get("progress_pct"),
            "queue_position": job.get("queue_position"),
        },
        "output": {
            "status": status,
            "external_status": external_status,
            "result": job.get("result"),
            "execution": job.get("execution"),
        },
        "payload": {"external": job},
    }
    out["target"] = build_target(
        resource_type="blast_job",
        job_id=dashboard_job_id or openapi_job_id,
        job_id_kind="dashboard" if dashboard_job_id else "openapi",
        dashboard_job_id=dashboard_job_id or None,
        openapi_job_id=openapi_job_id or None,
        links={
            "dashboard_status": f"/api/blast/jobs/{dashboard_job_id}"
            if dashboard_job_id
            else "",
            "openapi_status": f"/v1/jobs/{openapi_job_id}/status" if openapi_job_id else "",
        },
    )
    out.update(_external_execution_summary(job))
    infrastructure = {
        "subscription_id": metadata["subscription_id"],
        "resource_group": metadata["resource_group"],
        "cluster_name": metadata["cluster_name"],
        "storage_account": metadata["storage_account"],
    }
    if any(infrastructure.values()):
        out["infrastructure"] = {k: v for k, v in infrastructure.items() if v}
    if include_database_metadata:
        from api.services.blast.db_metadata import extract_trusted_storage_account

        # External-API jobs never populate infrastructure.storage_account, but
        # they carry the BLAST database as a full blob URL. Recover the account
        # (gated to the trusted workload account) so the same Storage-backed
        # resolver fills the sequence / letter counts and snapshot date
        # dashboard-submitted jobs show. The gate stops an attacker-influenced
        # db URL from leaking the MI Storage token to a foreign account.
        storage_account = str(
            infrastructure.get("storage_account") or ""
        ) or extract_trusted_storage_account(str(job.get("db") or ""))
        database_metadata = _database_metadata_for_response(
            db,
            storage_account,
        )
        if database_metadata is not None:
            out["database_metadata"] = database_metadata
    if error_message:
        out["error"] = error_message
    if error_code:
        out["error_code"] = error_code
    return out


def _database_metadata_for_response(
    database: str,
    storage_account: str,
) -> dict[str, Any] | None:
    try:
        from api.services.blast.db_metadata import resolve_database_display_metadata

        return resolve_database_display_metadata(storage_account, database)
    except Exception as exc:
        LOGGER.info(
            "database metadata projection skipped db=%s: %s",
            database,
            type(exc).__name__,
        )
        return None


def _external_result_files(job: dict[str, Any]) -> list[dict[str, Any]]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    files = result.get("files") if isinstance(result, dict) else []
    if not isinstance(files, list):
        return []
    out: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or item.get("name") or "")
        file_id = str(item.get("file_id") or "")
        if not filename or not file_id:
            continue
        out.append(
            {
                "file_id": file_id,
                "name": filename,
                "size": item.get("size_bytes") or item.get("size"),
                "last_modified": item.get("last_modified"),
                "format": item.get("format"),
                "source": "external",
            }
        )
    return out
