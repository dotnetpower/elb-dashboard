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
    "_enrich_external_failure_detail",
    "_external_error_message",
    "_external_execution_detail_text",
    "_external_execution_steps",
    "_external_execution_summary",
    "_external_result_files",
    "_external_status_to_dashboard",
    "_external_step_projection",
    "_external_to_blast_job",
    "_normalise_error_code",
    "_short_external_db_name",
]

# The dashboard's 8-step timeline (Prepare Run / Warmup Check / Configure /
# Stage DB / Submit Job / BLAST Run / Export / Complete) is a *dashboard-native*
# concept driven by the Celery submit task. A job that ran on the sibling
# OpenAPI plane (a ``/v1/jobs`` submit, whether direct or Service-Bus bridged)
# never executed the dashboard's node-local warmup / SSD-staging steps and the
# sibling does not report per-step progress. These two steps are therefore
# marked ``skipped`` (with an explicit reason) rather than fabricated as
# ``completed`` — surfacing them green would be a "looks-alive ≠ is-alive"
# lie. The remaining steps (prepare / configure / submit / run) are universally
# true for any ``elastic-blast`` run, so their state is derived honestly from
# the coarse external lifecycle (queued → running → success/failed/cancelled).
_EXTERNAL_DASHBOARD_ONLY_STEPS = ("warming_up", "staging_db")
_EXTERNAL_SKIP_REASON = "not_reported_by_external_api"
_STEP_DONE = {"status": "completed", "success": True, "source": "external_api"}
_STEP_SKIPPED = {
    "status": "skipped",
    "skipped": True,
    "skip_reason": _EXTERNAL_SKIP_REASON,
    "source": "external_api",
}


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
    """Collapse whitespace, redact secrets, and cap a free-form error message.

    The message ends up in the dashboard banner and (via the synthesized step
    projection) in ``output.steps[failed].error`` / ``.output``, so it MUST be
    sanitised (Charter §12: UI-shown output is sanitised). A sibling
    elastic-blast failure body can embed a SAS query string, a Bearer token, or
    a subscription GUID; ``sanitise`` masks all three. ANSI control sequences
    are stripped by ``sanitise`` too, before the whitespace collapse.
    """
    from api.services.sanitise import sanitise

    collapsed = " ".join(sanitise(str(raw)).split())
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


def _external_execution_detail_text(
    job: dict[str, Any],
    execution_summary: dict[str, int],
) -> str:
    """Render the *real* sibling-reported execution detail as a log block.

    Only genuinely-reported fields are included (BLAST+ version, DB version,
    shard counts, hit count). Returns ``""`` when the sibling reported nothing,
    so callers never attach an empty/fabricated console block to a step.
    """
    execution = job.get("execution")
    if not isinstance(execution, dict):
        result_exec = job.get("result")
        if isinstance(result_exec, dict) and isinstance(result_exec.get("execution"), dict):
            execution = result_exec["execution"]
    execution = execution if isinstance(execution, dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}

    def _first(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    lines: list[str] = []
    blast_version = _first(job.get("blast_version"), execution.get("blast_version"))
    if blast_version:
        lines.append(f"BLAST+ version : {blast_version}")
    db_version = _first(
        job.get("db_version"), execution.get("db_version"), execution.get("db_last_updated")
    )
    if db_version:
        lines.append(f"DB version     : {db_version}")
    total = execution_summary.get("splits_total")
    done = execution_summary.get("splits_done")
    failed = execution_summary.get("splits_failed")
    if total:
        lines.append(
            f"Shards         : {done or 0}/{total} done"
            + (f", {failed} failed" if failed else "")
        )
    hit_count = result.get("hit_count")
    if isinstance(hit_count, int):
        lines.append(f"Hits           : {hit_count}")
    return "\n".join(lines)


def _external_execution_steps(
    *,
    dashboard_status: str,
    error_message: str | None,
    execution_summary: dict[str, int],
    detail_text: str = "",
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Synthesize an honest step-state map for an external OpenAPI job.

    The sibling only reports a coarse ``queued → running → success/failed/
    cancelled`` lifecycle, so we derive the dashboard timeline states from it
    WITHOUT fabricating per-step console output. Dashboard-only node-local
    steps (warmup / SSD staging) are marked ``skipped`` because the external
    API neither runs nor reports them. On failure the real error is attached to
    the inferred failed step (``running`` when shard activity is visible, else
    ``submitting``).

    Returns ``(steps, failed_step_key)`` — ``failed_step_key`` is ``None`` for
    non-failure lifecycles.
    """
    steps: dict[str, dict[str, Any]] = {
        step: dict(_STEP_SKIPPED) for step in _EXTERNAL_DASHBOARD_ONLY_STEPS
    }

    if dashboard_status == "queued":
        steps["preparing"] = {"status": "running", "source": "external_api"}
        return steps, None

    if dashboard_status == "running":
        steps["preparing"] = dict(_STEP_DONE)
        steps["configuring"] = dict(_STEP_DONE)
        steps["submitting"] = dict(_STEP_DONE)
        running = {"status": "running", "source": "external_api"}
        if detail_text:
            running["last_output"] = detail_text
        steps["running"] = running
        return steps, None

    if dashboard_status == "completed":
        for key in ("preparing", "configuring", "submitting", "exporting_results", "completed"):
            steps[key] = dict(_STEP_DONE)
        running = dict(_STEP_DONE)
        if detail_text:
            running["last_output"] = detail_text
        steps["running"] = running
        return steps, None

    if dashboard_status in {"failed", "cancelled"}:
        # Infer where it broke: any visible shard activity means it reached the
        # BLAST run; otherwise the failure is at submit time.
        had_run_activity = any(
            execution_summary.get(key, 0)
            for key in ("splits_total", "splits_done", "splits_failed", "splits_active")
        )
        failed_step = "running" if had_run_activity else "submitting"
        steps["preparing"] = dict(_STEP_DONE)
        steps["configuring"] = dict(_STEP_DONE)
        if failed_step == "running":
            steps["submitting"] = dict(_STEP_DONE)
        if dashboard_status == "failed":
            failure_step: dict[str, Any] = {
                "status": "failed",
                "success": False,
                "source": "external_api",
            }
            if error_message:
                failure_step["error"] = error_message
                failure_step["output"] = error_message
            steps[failed_step] = failure_step
            return steps, failed_step
        # Cancelled is terminal but NOT a failure phase: leave the stopped step
        # skipped (with a cancellation reason) and do not flag a failed step.
        steps[failed_step] = {
            "status": "skipped",
            "skipped": True,
            "skip_reason": "cancelled",
            "source": "external_api",
        }
        return steps, None

    # Unknown / unmapped status — surface nothing fabricated.
    return steps, None


_EXTERNAL_FAILED_NO_DETAIL = (
    "External BLAST job failed, but the OpenAPI service reported no error "
    "detail. Check the sibling job logs for the underlying cause."
)

# Coarse, non-actionable failure strings the sibling OpenAPI service stamps when
# it only knows the Kubernetes Job failed (it does not read the runner's
# blastn stderr / exit code). When the current error is one of these (or empty /
# the no-detail placeholder) the dashboard prefers the authoritative cluster-side
# ``FAILURE.txt`` / ``BLAST_RUNTIME`` detail if it can read it from Storage.
_EXTERNAL_GENERIC_FAILURE_MESSAGES = frozenset(
    {
        "BLAST job failed",
        "one or more BLAST jobs failed",
        "submit job failed before creating BLAST jobs",
    }
)

# Kubernetes pod/container *state* substrings the sibling sometimes stamps as
# the job ``error`` (e.g. ``pod <name> container blast is CrashLoopBackOff``).
# These read as "specific" because they are not in the exact-match generic set
# above, yet they are NOT actionable: they describe the K8s symptom, not the
# blastn cause (a DB that is not staged on the node, an out-of-memory kill, an
# image that cannot be pulled). Treating them as coarse lets the dashboard
# prefer the authoritative ``FAILURE.txt`` / ``BLAST_RUNTIME`` detail when it
# can read one — exactly the case a live E2E surfaced (2026-06-16): a 16S search
# whose DB was not re-warmed on a fresh node after a cluster stop/start failed
# with a bare ``CrashLoopBackOff`` and no underlying reason.
_EXTERNAL_COARSE_K8S_FAILURE_SUBSTRINGS = (
    "crashloopbackoff",
    "imagepullbackoff",
    "errimagepull",
    "oomkilled",
    "is not ready",
    "is not running",
    "backoff limit",
    "backofflimitexceeded",
    "container blast is",
    "pod is not",
)


def _is_coarse_k8s_failure(message: str | None) -> bool:
    """True when ``message`` is a non-actionable K8s pod/container-state string.

    Such a message (e.g. ``CrashLoopBackOff``) names the Kubernetes symptom but
    not the blastn root cause, so the dashboard should still prefer the
    authoritative cluster-side ``FAILURE.txt`` detail. Case-insensitive
    substring match against ``_EXTERNAL_COARSE_K8S_FAILURE_SUBSTRINGS``.
    """
    if not message:
        return False
    lowered = message.casefold()
    return any(token in lowered for token in _EXTERNAL_COARSE_K8S_FAILURE_SUBSTRINGS)



def _enrich_external_failure_detail(
    *,
    status: str,
    current_error: str | None,
    storage_account: str,
    results_job_id: str,
    steps: dict[str, Any] | None = None,
    failed_step: str | None = None,
) -> str | None:
    """Recover the cluster-side blastn failure detail for a failed external job.

    The sibling OpenAPI service only reports a coarse ``failed`` lifecycle with a
    generic (or empty) ``error``. The authoritative blastn diagnostics live in
    the workload results container (``metadata/FAILURE.txt`` +
    ``logs/BLAST_RUNTIME-NNN.out``), which ``read_blast_runtime_failure`` reads
    best-effort. This is gated by the caller to detail-view renders only (it
    performs a Storage list + small blob reads), and only fires when the current
    error is missing / a known-generic sibling string — a genuinely specific
    sibling error is left untouched.

    Returns the sanitised + clamped replacement detail (also mutating the failed
    step's ``error`` / ``output`` in place when ``steps``/``failed_step`` are
    given), or ``None`` when nothing better is available.
    """
    if status != "failed":
        return None
    has_specific = bool(
        current_error
        and current_error != _EXTERNAL_FAILED_NO_DETAIL
        and current_error not in _EXTERNAL_GENERIC_FAILURE_MESSAGES
        and not _is_coarse_k8s_failure(current_error)
    )
    if has_specific:
        return None
    if not storage_account or not results_job_id:
        return None
    from api.services.blast.runtime_failure import read_blast_runtime_failure

    raw_detail = read_blast_runtime_failure(storage_account, results_job_id)
    detail = _clamp_error_message(raw_detail) if raw_detail else None
    if not detail:
        return None
    if steps is not None and failed_step and isinstance(steps.get(failed_step), dict):
        steps[failed_step]["error"] = detail
        steps[failed_step]["output"] = detail
    return detail


def _external_step_projection(
    job: dict[str, Any],
    *,
    dashboard_status: str,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Shared honest step + error projection for an external OpenAPI job.

    Used by BOTH ``_external_to_blast_job`` (fresh sibling fetch) and the
    external-origin branch of ``_local_to_blast_job`` (a synced Table row whose
    payload embeds the sibling job under ``payload['external']``) so the two
    code paths surface an identical timeline.

    ``dashboard_status`` is the CURRENT normalised status — for a synced row the
    embedded snapshot's raw ``status`` can be stale, so the live row status is
    passed in explicitly. Returns ``steps`` / ``error`` / ``failed_step`` /
    ``execution_summary`` (the shard tallies the caller also merges top-level).
    """
    if error_message is None:
        _code, error_message = _external_error_message(job.get("error"))
    if dashboard_status == "failed" and not error_message:
        error_message = _EXTERNAL_FAILED_NO_DETAIL
    execution_summary = _external_execution_summary(job)
    detail_text = _external_execution_detail_text(job, execution_summary)
    steps, failed_step = _external_execution_steps(
        dashboard_status=dashboard_status,
        error_message=error_message,
        execution_summary=execution_summary,
        detail_text=detail_text,
    )
    return {
        "steps": steps,
        "error": error_message,
        "failed_step": failed_step,
        "execution_summary": execution_summary,
    }


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
    # canonical_job_metadata defaults ``program`` to ``"blast"`` and builds
    # ``job_title`` from program + db + query_label. For an external sibling row
    # that did not yet send program/db/query (the typical state right after the
    # sibling's /v1/jobs row appears), the title collapses to the literal
    # string ``"blast"`` -- which then propagates to the dashboard list and the
    # Recent searches card (issue #4). Replace that degenerate title with the
    # openapi job id so the row stays identifiable, and pass the sentinel
    # through to the create path so the same fallback works on first sync.
    explicit_title = str(job.get("job_title") or job.get("title") or "").strip()
    has_real_meta = bool(
        explicit_title
        or job.get("program")
        or job.get("db_name")
        or job.get("db")
        or job.get("query_file")
        or job.get("query")
    )
    if not has_real_meta and metadata["job_title"] == "blast":
        fallback_id = str(job.get("job_id") or "").strip()
        metadata["job_title"] = (
            f"External job {fallback_id[:12]}" if fallback_id else "External job"
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
    error_code, _raw_error_message = _external_error_message(job.get("error"))
    # A failed external job must never surface as "No detailed error was
    # recorded": the shared projection synthesizes an honest, non-empty message
    # when the sibling reports a failure with no usable error body (mirror of
    # the local submit-failed fix in api/tasks/blast/cli_parsing.py). It also
    # builds the honest step timeline (dashboard-only warmup/staging steps are
    # skipped, never faked as completed).
    projection = _external_step_projection(
        job, dashboard_status=status, error_message=_raw_error_message
    )
    steps = projection["steps"]
    failed_step = projection["failed_step"]
    error_message = projection["error"]
    execution_summary = projection["execution_summary"]
    output: dict[str, Any] = {
        "status": status,
        "external_status": external_status,
        "result": job.get("result"),
        "execution": job.get("execution"),
        "steps": steps,
    }
    if error_message:
        output["error"] = error_message
    if failed_step:
        output["failed_step"] = failed_step
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
        "queue_origin": str(job.get("queue_origin") or ""),
        "config_snapshot": job.get("config_snapshot")
        if isinstance(job.get("config_snapshot"), dict) and job.get("config_snapshot")
        else None,
        "db_version": str(job.get("db_version") or "") or None,
        "blast_version": str(job.get("blast_version") or "") or None,
        "run_seconds": job.get("run_seconds"),
        "queue_wait_seconds": job.get("queue_wait_seconds"),
        "elapsed_seconds": job.get("elapsed_seconds"),
        "external_correlation_id": job.get("external_correlation_id") or "",
        "query_label": metadata["query_label"] or "query.fa",
        "owner_upn": "api",
        "custom_status": {
            "phase": status,
            "blast_status": external_status,
            "progress_pct": job.get("progress_pct"),
            "queue_position": job.get("queue_position"),
            "steps": steps,
        },
        "output": output,
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
    out.update(execution_summary)
    # The sibling /v1/jobs payload never populates infrastructure.storage_account,
    # but it always carries the BLAST database as a full blob URL. Derive the
    # account from the URL (gated to the trusted workload account) so the row
    # synced into Azure Table Storage by ``_sync_external_jobs_to_table`` ends up
    # with a non-empty ``storage_account`` column. Without this, every later
    # ``/jobs/{id}/file`` / ``/results/alignments`` call enters the
    # "JobState has no recorded account" fallback path on each request
    # (noisy INFO logs) and the storage cross-check cannot enforce its
    # cross-account guard. The trust gate stops an attacker-influenced db URL
    # from leaking the MI Storage token to a foreign account.
    from api.services.blast.db_metadata import extract_trusted_storage_account

    derived_storage_account = extract_trusted_storage_account(str(job.get("db") or ""))
    infrastructure = {
        "subscription_id": metadata["subscription_id"],
        "resource_group": metadata["resource_group"],
        "cluster_name": metadata["cluster_name"],
        "storage_account": metadata["storage_account"] or derived_storage_account,
    }
    # Resolve the AKS region (best-effort) so the detail shows it instead of
    # "—". Prefer a value the drain already stamped on the row (hardening round
    # 5 — keeps the ARM call off the list hot path); only resolve live (1h
    # cached) when the row carries none. The sibling /v1/jobs record never
    # carries a region; it is constant per cluster.
    _stamped_region = str(job.get("region") or "").strip()
    if _stamped_region:
        infrastructure["region"] = _stamped_region
    else:
        try:
            from api.services.blast.external_config import resolve_cluster_region

            _region = resolve_cluster_region(
                str(metadata["subscription_id"] or ""),
                str(metadata["resource_group"] or ""),
                str(metadata["cluster_name"] or ""),
            )
            if _region:
                infrastructure["region"] = _region
        except Exception:  # pragma: no cover - best-effort, region just stays "—"
            LOGGER.debug("external job region resolve skipped", exc_info=True)
    if any(infrastructure.values()):
        out["infrastructure"] = {k: v for k, v in infrastructure.items() if v}
    if include_database_metadata:
        # External-API jobs never populate infrastructure.storage_account, but
        # they carry the BLAST database as a full blob URL. Recover the account
        # (gated to the trusted workload account) so the same Storage-backed
        # resolver fills the sequence / letter counts and snapshot date
        # dashboard-submitted jobs show. The gate stops an attacker-influenced
        # db URL from leaking the MI Storage token to a foreign account.
        storage_account = str(
            infrastructure.get("storage_account") or ""
        ) or derived_storage_account
        database_metadata = _database_metadata_for_response(
            db,
            storage_account,
        )
        if database_metadata is not None:
            out["database_metadata"] = database_metadata
        # When the sibling reported a failure with no usable detail, recover the
        # authoritative cluster-side blastn error from the results container
        # (gated to this detail-view path so list rendering never pays for it).
        # External jobs store results under ``results/<openapi_job_id>/...``.
        enriched_error = _enrich_external_failure_detail(
            status=status,
            current_error=error_message,
            storage_account=storage_account,
            results_job_id=openapi_job_id,
            steps=steps,
            failed_step=failed_step,
        )
        if enriched_error:
            error_message = enriched_error
            output["error"] = enriched_error
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
        entry = {
            "file_id": file_id,
            "name": filename,
            "size": item.get("size_bytes") or item.get("size"),
            "last_modified": item.get("last_modified"),
            "format": item.get("format"),
            "source": "external",
        }
        # The sibling exposes the result blob's path relative to
        # ``results/{job_id}/`` (``_list_result_files`` → ``blob_path``). Carry it
        # through so the dashboard can persist a file_id → blob_path manifest and
        # stream the result straight from Storage when the elb-openapi proxy is
        # unreachable (cluster auto-stopped) — see the Service Bus completion
        # manifest persistence + the download Storage fallback.
        blob_path = str(item.get("blob_path") or "").strip()
        if blob_path:
            entry["blob_path"] = blob_path
        out.append(entry)
    return out
