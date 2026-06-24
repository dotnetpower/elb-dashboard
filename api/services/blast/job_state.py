"""BLAST job projection, file preview, and refresh helpers.

Responsibility: BLAST job projection, file preview, and refresh helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `_payload_value`, `_queries_blob_path`, `_job_query_blob_path`,
`_refresh_running_blast_state`, `_blocked_refresh_reasons`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import logging
import os
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from api.auth import CallerIdentity
from api.services.response_contracts import build_target

LOGGER = logging.getLogger(__name__)

from api.services.blast.external_jobs import (  # noqa: E402
    _EXTERNAL_DETAIL_ENRICH_LIMIT as _EXTERNAL_DETAIL_ENRICH_LIMIT,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _EXTERNAL_NOT_ENABLED_REASONS as _EXTERNAL_NOT_ENABLED_REASONS,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _exception_reason as _exception_reason,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_job_detail_or_row as _external_job_detail_or_row,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_list_jobs_cached as _external_list_jobs_cached,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_result_files as _external_result_files,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_status_to_dashboard as _external_status_to_dashboard,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _external_to_blast_job as _external_to_blast_job,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _merge_external_detail as _merge_external_detail,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _openapi_client_kwargs_from_cluster as _openapi_client_kwargs_from_cluster,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _reset_external_jobs_cache as _reset_external_jobs_cache,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _short_external_db_name as _short_external_db_name,
)
from api.services.blast.external_jobs import (  # noqa: E402
    _sync_external_jobs_to_table as _sync_external_jobs_to_table,
)
from api.services.blast.runtime_failure import (  # noqa: E402
    read_blast_runtime_failure as _read_blast_runtime_failure,
)


def _payload_value(payload: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _queries_blob_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("az://"):
        raw = "https://" + raw.removeprefix("az://")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and parts[0] == "queries":
            return parts[1]
        return ""
    raw = raw.lstrip("/")
    if raw.startswith("queries/"):
        raw = raw.removeprefix("queries/")
    return raw


def _job_query_blob_path(job_id: str, caller: CallerIdentity) -> str:
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception as exc:
        LOGGER.info("query preview state lookup failed job_id=%s: %s", job_id, type(exc).__name__)
        return ""
    if state is None:
        return ""
    if getattr(state, "owner_oid", None) and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    blob_path = _queries_blob_path(_payload_value(payload, "query_file", "query_blob_url"))
    if not blob_path:
        # External (OpenAPI / Service Bus) jobs carry no top-level query field
        # on the job row: the sibling elastic-blast-azure plane uploads the
        # inline FASTA to ``queries/<openapi_id>.fa`` and records nothing back.
        # Without this fallback the prepare-step query preview (and any other
        # ``input.fa`` reader) resolves to ``<job_id>/input.fa`` — a path that
        # never exists for these jobs — so the Run details panel renders
        # "Could not load input.fa" even though the query is in Storage. Mirror
        # the reconstruction ``blast_job_query`` (Edit search) already does so
        # both surfaces agree. The OpenAPI id from the external payload is
        # authoritative; the route ``job_id`` matches it for synced rows.
        external_payload = (
            payload.get("external") if isinstance(payload.get("external"), dict) else None
        )
        if external_payload is not None:
            openapi_id = str(external_payload.get("job_id") or job_id).strip()
            if openapi_id and "/" not in openapi_id and ".." not in openapi_id:
                blob_path = f"{openapi_id}.fa"
    return blob_path


def derive_external_query_label(job_id: str, caller: CallerIdentity) -> str:
    """Durable Query ID fallback for an external job with no remembered label.

    The inline-FASTA defline label is remembered in OPS Redis (ephemeral, wiped
    on every Container App revision restart), so an external job viewed after a
    restart shows ``Query ID: —``. This reads the first FASTA defline from the
    DURABLE query blob (``queries/<openapi_id>.fa`` — the same blob the
    prepare-step preview reads) and derives a short label, so the header
    recovers the identity from Storage instead of the lost cache.

    Detail-view only (one small Storage read, capped to 512 bytes) — never use
    on the jobs LIST path (it would be one read per row). Returns ``""`` for a
    non-external job, an unresolvable/unreadable blob, or a header-less FASTA;
    never raises except the ownership 403 (mirrors ``_job_query_blob_path``).
    """
    blob_path = _job_query_blob_path(job_id, caller)
    if not blob_path:
        return ""
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception:
        return ""
    if state is None:
        return ""
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    external_payload = (
        payload.get("external") if isinstance(payload.get("external"), dict) else None
    )
    if external_payload is None:
        # Dashboard jobs already carry a query_label from their payload; the
        # durable-blob fallback exists only for external (OpenAPI) jobs.
        return ""
    storage_account = getattr(state, "storage_account", "") or str(
        _payload_value(payload, "storage_account") or ""
    )
    if not storage_account:
        from api.services.blast.db_metadata import extract_trusted_storage_account

        storage_account = extract_trusted_storage_account(
            str(getattr(state, "db", "") or "")
        ) or extract_trusted_storage_account(str(external_payload.get("db") or ""))
    if not storage_account:
        return ""
    try:
        from api.services import get_credential
        from api.services.storage.data import read_blob_text

        text = read_blob_text(
            get_credential(),
            storage_account,
            container="queries",
            blob_path=blob_path,
            max_bytes=512,
        )
    except Exception as exc:
        LOGGER.debug(
            "derive_external_query_label read failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        return ""
    from api.services.blast.external_query_labels import derive_inline_query_label

    try:
        return derive_inline_query_label(text)
    except Exception:
        return ""


def _blob_not_found(exc: BaseException) -> bool:
    from azure.core.exceptions import ResourceNotFoundError

    if isinstance(exc, ResourceNotFoundError):
        return True
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "BlobNotFound",
            "ResourceNotFound",
            "The specified blob does not exist",
        )
    )


def _job_payload_for_file_preview(job_id: str, caller: CallerIdentity) -> dict[str, Any]:
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception as exc:
        if os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true":
            LOGGER.info(
                "file preview state lookup skipped job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
            return {}
        LOGGER.warning("file preview state lookup failed job_id=%s: %s", job_id, type(exc).__name__)
        raise HTTPException(503, {"code": "state_lookup_unavailable"}) from exc
    if state is None:
        return {}
    if getattr(state, "owner_oid", None) and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")
    raw_payload = getattr(state, "payload", None)
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    return payload


def _enrich_preview_outfmt(options: dict[str, Any]) -> None:
    """Reflect the result-UI parity columns in the Configure preview, idempotently.

    The actual run already carries them (``enrich_tabular_outfmt`` runs at
    submit), but jobs recorded before that shipped — or whose stored options
    keep a bare ``6`` / ``7`` — would render a Configure preview without the
    Description / Scientific-name / Query-Cover columns the result page reads.
    The extended layout is parked in ``additional_options`` (``-outfmt 7 std
    …``) because the bare ``outfmt`` field rejects a multi-token specifier.
    """
    from api.services.sharding_precision import (
        enrich_tabular_outfmt,
        outfmt_spec_value,
        set_outfmt_spec,
    )

    additional = str(options.get("additional_options") or "")
    current = outfmt_spec_value(additional) if additional.strip() else None
    if current is None:
        bare = options.get("outfmt")
        current = str(bare).strip() if bare not in (None, "") else None
    if not current:
        return
    enriched = enrich_tabular_outfmt(current)
    if not isinstance(enriched, str) or enriched == current:
        return
    options["additional_options"] = set_outfmt_spec(additional, enriched).strip()
    # The enriched tabular spec is multi-token; keep a single ``-outfmt`` in the
    # rendered ini by dropping any conflicting bare ``outfmt`` field.
    options.pop("outfmt", None)


def _apply_existing_cluster_shape(
    options: dict[str, Any],
    *,
    credential: Any,
    subscription_id: str | None,
    resource_group: str,
    cluster_name: str,
    job_id: str,
) -> None:
    """Reflect the existing cluster's blastpool SKU / node count in the preview.

    Externally-submitted jobs reuse an already-provisioned cluster, so the
    Configure preview should show that cluster's machine-type / num-nodes
    ("the current cluster as-is") rather than the generic ``DEFAULT_SKU`` /
    ``num-nodes = 1`` fallbacks ``generate_config`` applies when the options
    omit them. Best-effort: a resolution failure leaves the prior behaviour.
    """
    if credential is None:
        return
    sub = (subscription_id or "").strip() or os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not (sub and resource_group and cluster_name):
        return
    try:
        from api.services.monitoring.aks import get_aks_cluster_snapshot

        snapshot = get_aks_cluster_snapshot(credential, sub, resource_group, cluster_name)
    except Exception:
        # Best-effort preview enrichment: a lookup failure keeps the fallback.
        LOGGER.debug("config preview cluster lookup failed job_id=%s", job_id, exc_info=True)
        return
    if not snapshot:
        return
    sku = snapshot.get("node_sku")
    if isinstance(sku, str) and sku.strip():
        options["machine_type"] = sku.strip()
    count = snapshot.get("node_count")
    if isinstance(count, int) and count > 0:
        options["num_nodes"] = count


def _config_preview_from_payload(
    *,
    job_id: str,
    storage_account: str,
    payload: dict[str, Any],
    credential: Any = None,
    subscription_id: str | None = None,
) -> str:
    from api.tasks.blast import _build_config_content

    # Externally-submitted jobs (sibling OpenAPI / Service Bus -> Table sync)
    # do not carry top-level ``db`` / ``query_file`` / ``resource_group`` /
    # ``cluster_name`` keys: the run identity lives under
    # ``payload["canonical_request"]`` (the canonical submit snapshot) and
    # ``payload["external"]`` instead. Reading only the top-level keys made the
    # Configure preview render ``db =`` / ``queries =`` / ``azure-resource-group =``
    # blank for those jobs. Resolve every field through the same fallback chain
    # the provenance + query-preview projections already use so the preview
    # matches what actually ran.
    snapshot = payload.get("canonical_request")
    if not isinstance(snapshot, dict):
        from api.services.blast.submit_payload import canonical_submit_snapshot

        snapshot = canonical_submit_snapshot(payload)
    external = payload.get("external")
    external = external if isinstance(external, dict) else {}

    raw_options = payload.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    for key in ("acr_resource_group", "acr_name"):
        value = payload.get(key)
        if value not in (None, ""):
            options.setdefault(key, value)

    def _first(*values: Any) -> str:
        for value in values:
            if value not in (None, ""):
                return str(value)
        return ""

    database = _first(
        snapshot.get("database"),
        _payload_value(payload, "database", "db"),
        external.get("db_name"),
        external.get("db"),
    )
    resource_group = _first(
        snapshot.get("resource_group"),
        _payload_value(payload, "resource_group"),
        external.get("resource_group"),
    )
    cluster_name = _first(
        snapshot.get("cluster_name"),
        snapshot.get("aks_cluster_name"),
        _payload_value(payload, "cluster_name", "aks_cluster_name"),
        external.get("cluster_name"),
        external.get("cluster"),
    )
    query_file = _first(
        _payload_value(payload, "query_file", "query_blob_url"),
        external.get("query_url"),
        external.get("query_file"),
    )
    if not query_file:
        # External jobs upload the inline FASTA to ``queries/<openapi_id>.fa``
        # and record nothing on the top-level row; reconstruct that path (the
        # same one ``_job_query_blob_path`` resolves) so ``queries =`` is
        # populated rather than blank.
        openapi_id = str(external.get("job_id") or "").strip()
        if openapi_id and "/" not in openapi_id and ".." not in openapi_id:
            query_file = f"{openapi_id}.fa"

    _enrich_preview_outfmt(options)
    _apply_existing_cluster_shape(
        options,
        credential=credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        job_id=job_id,
    )

    return _build_config_content(
        job_id=job_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
        storage_account=_first(_payload_value(payload, "storage_account"), storage_account),
        program=_first(snapshot.get("program"), _payload_value(payload, "program")) or "blastn",
        database=database,
        query_file=query_file,
        options=options,
    )


_PROGRESS_STEP_ORDER = (
    "preparing",
    "warming_up",
    "configuring",
    "staging_db",
    "submitting",
    "running",
    "exporting_results",
    "completed",
)

_K8S_REFRESH_PHASES = frozenset(
    {
        "submitted",
        "running",
        "results_pending",
    }
)
# Per-phase throttle. `submitted` keeps the original 20 s floor because the K8s
# job may not exist yet immediately after `elastic-blast submit` returns and
# repeated misses are wasteful. `running` and `results_pending` are the hot
# phases where the BLAST container is either close to or already finished, so
# we tighten the floor to 5 s — that turns the perceived "K8s finished →
# dashboard catches up" latency from ~20 s (or 60 s via beat) into ~5 s.
_K8S_REFRESH_MIN_INTERVAL_SECONDS = 20.0
_K8S_REFRESH_FAST_INTERVAL_SECONDS = 5.0
_K8S_REFRESH_FAST_PHASES = frozenset({"running", "results_pending"})
_K8S_REFRESH_LAST_CHECK: dict[tuple[str, str, str, str], float] = {}
# Cluster-level negative cache for the live K8s status refresh. When the K8s API
# for a (subscription, resource_group, cluster) is unreachable — the cluster is
# auto-stopped, the API-server cert is failing the TLS handshake, or a transient
# network blip — each refresh blocks on three serial GETs at `timeout=10`
# (namespace + pods + jobs) before `k8s_check_blast_status` maps the failure to
# `status="unknown"`. The per-job `_K8S_REFRESH_LAST_CHECK` throttle only
# suppresses that one job for a few seconds, so opening several running jobs'
# detail views each paid the full ~27 s. Remember an unreachable cluster here so
# every sibling job skips the slow call until the cooldown lapses; the next
# reachable refresh clears it so recovery is immediate.
_K8S_REFRESH_FAILURE_COOLDOWN_SECONDS = max(
    5.0, float(os.environ.get("BLAST_K8S_REFRESH_FAILURE_COOLDOWN_SECONDS", "60") or "60")
)
_K8S_REFRESH_CLUSTER_COOLDOWN: dict[tuple[str, str, str], float] = {}
_NON_ERROR_RUNNING_ERROR_CODES = frozenset({"blast_submit_lock_busy"})


def _refresh_min_interval_seconds(phase: str) -> float:
    if phase in _K8S_REFRESH_FAST_PHASES:
        return _K8S_REFRESH_FAST_INTERVAL_SECONDS
    return _K8S_REFRESH_MIN_INTERVAL_SECONDS


def _arm_cluster_refresh_cooldown(
    cluster_key: tuple[str, str, str], cluster_name: str, now: float, reason: str
) -> None:
    """Arm the cluster-level negative cache, logging once per outage episode.

    Dedups by logging only when the cluster was not already cooling down, so a
    sustained outage (re-armed on every refresh) yields a single INFO line
    instead of a flood. A recovered+re-failed cluster logs again because the
    success path pops the key first.
    """
    if cluster_key not in _K8S_REFRESH_CLUSTER_COOLDOWN:
        LOGGER.info(
            "blast k8s refresh: cluster '%s' unreachable (%s); cooling down live "
            "refresh for %.0fs",
            cluster_name,
            reason,
            _K8S_REFRESH_FAILURE_COOLDOWN_SECONDS,
        )
    _K8S_REFRESH_CLUSTER_COOLDOWN[cluster_key] = now + _K8S_REFRESH_FAILURE_COOLDOWN_SECONDS


def _maybe_reload_with_payload(repo: Any, state: Any) -> Any:
    """Reload a row from the repo when its payload was omitted (list path).

    The list endpoint pulls rows with ``include_payload=False`` to keep the
    response small, but mutating the row (transitioning to results_pending /
    completed / failed) needs the existing ``_progress`` to merge step history
    rather than clobber it. This helper returns the original state on any
    error — tests use simplified Repo doubles without ``.get()``.
    """
    payload = getattr(state, "payload", None)
    if isinstance(payload, dict) and payload:
        return state
    get = getattr(repo, "get", None)
    if get is None:
        return state
    try:
        full = get(state.job_id)
    except Exception as exc:
        LOGGER.debug(
            "blast refresh payload reload skipped job_id=%s: %s",
            getattr(state, "job_id", ""),
            type(exc).__name__,
        )
        return state
    return full if full is not None else state


def _split_child_summary_from_repo(repo: Any, parent_job_id: str) -> dict[str, Any] | None:
    try:
        children = list(repo.list_children(parent_job_id, limit=1000))
    except Exception as exc:
        LOGGER.info(
            "split child summary unavailable job_id=%s: %s", parent_job_id, type(exc).__name__
        )
        return None
    if not children:
        return None
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for child in children:
        status = str(getattr(child, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        items.append(
            {
                "job_id": getattr(child, "job_id", ""),
                "status": status,
                "phase": getattr(child, "phase", None),
                "group_id": payload.get("group_id"),
                "query_file": payload.get("query_file"),
                "effective_search_space": payload.get("effective_search_space"),
            }
        )
    return {"child_count": len(children), "children_by_status": counts, "children": items}


def _split_child_summary_from_children(children: list[Any]) -> dict[str, Any] | None:
    if not children:
        return None
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for child in children:
        status = str(getattr(child, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        payload = child.payload if isinstance(getattr(child, "payload", None), dict) else {}
        items.append(
            {
                "job_id": getattr(child, "job_id", ""),
                "status": status,
                "phase": getattr(child, "phase", None),
                "group_id": payload.get("group_id"),
                "query_file": payload.get("query_file"),
                "effective_search_space": payload.get("effective_search_space"),
            }
        )
    return {"child_count": len(children), "children_by_status": counts, "children": items}


def _split_child_summaries_from_repo(
    repo: Any,
    owner_oid: str,
    parent_job_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not parent_job_ids:
        return {}
    try:
        grouped = repo.list_children_for_owner(owner_oid, parent_job_ids, limit=5000)
    except AttributeError:
        return {
            parent_job_id: summary
            for parent_job_id in parent_job_ids
            if (summary := _split_child_summary_from_repo(repo, parent_job_id)) is not None
        }
    except Exception as exc:
        LOGGER.info("split child summaries unavailable: %s", type(exc).__name__)
        return {}
    summaries: dict[str, dict[str, Any]] = {}
    for parent_job_id, children in grouped.items():
        summary = _split_child_summary_from_children(children)
        if summary is not None:
            summaries[parent_job_id] = summary
    return summaries


def _job_error_for_response(state: Any) -> str:
    error_code = str(getattr(state, "error_code", "") or "")
    payload = getattr(state, "payload", None)
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("error") or "").strip()
    status = str(getattr(state, "status", "") or "").strip().casefold()
    # A successfully-completed job has no user-facing error. A stale
    # error_code / payload.error can linger on the row when a job was
    # transiently demoted (e.g. `worker_lost` while the submit worker died
    # mid-flight) and then later reconciled to `completed` once its results
    # were detected: the reconcile / artifact-finalize paths flip status+phase
    # but do NOT always clear the error fields, so the Run details page painted
    # a red error on a job that actually succeeded. Suppress here (before any
    # other branch) — the success banner already represents the outcome.
    if status == "completed":
        return ""
    if not error_code:
        # No machine code, but a human failure detail mirrored to payload.error
        # is still worth surfacing rather than reporting no error at all.
        return detail
    if status == "running" and error_code in _NON_ERROR_RUNNING_ERROR_CODES:
        return ""
    # When a human-readable detail accompanies an opaque machine code (the
    # `_retry_or_fail` final-failure path: error_code="terminal_az_login_failed"
    # + payload.error=<the actual az/kubectl error>), show BOTH so the operator
    # sees the classification AND the reason. The submit-failed path already
    # stores the full text as error_code with no separate payload.error, so it
    # returns the detailed text unchanged.
    if detail and detail != error_code and detail not in error_code:
        return f"{error_code}: {detail}"
    return error_code


def _resolve_local_submission_source(
    payload: dict[str, Any], is_external_origin: bool, *, column: str | None = None
) -> str:
    """Resolve the submission_source for a locally-stored job row.

    Prefers the durable ``submission_source`` column (populated for queue-drained
    rows so the list view, which reads columns only with
    ``include_payload=False``, surfaces the true queue origin instead of the
    ``"dashboard"`` default), then the nested ``payload.external.submission_source``
    (queue-drained shared rows stamp ``"servicebus"`` there) and the payload top
    level (the send-time ``servicebus`` placeholder stamps it there). Falls back
    to ``"external_api"`` for external-origin rows and ``"dashboard"`` otherwise
    so the field is always populated and never silently drops a queue origin.
    """
    col = str(column or "").strip()
    if col:
        return col
    external = payload.get("external") if isinstance(payload, dict) else None
    if isinstance(external, dict):
        nested = str(external.get("submission_source") or "").strip()
        if nested:
            return nested
    top = str(payload.get("submission_source") or "").strip() if isinstance(payload, dict) else ""
    if top:
        return top
    return "external_api" if is_external_origin else "dashboard"


def _resolve_local_queue_origin(payload: dict[str, Any], *, column: str | None = None) -> str:
    """Resolve queue_origin (``control_plane`` | ``external`` | "") for a local row.

    Prefers the durable ``queue_origin`` column (populated for queue rows so the
    list view, which reads columns only with ``include_payload=False``, surfaces
    the origin). A send-time placeholder row is written ONLY by the control-plane
    send route, so a row carrying ``payload.placeholder`` is control-plane even
    before the drain stamps the durable value. Otherwise prefer the nested
    ``payload.external.queue_origin`` (drained shared rows) then the payload top
    level. Empty string for rows with no queue origin (UI/API submits).
    """
    col = str(column or "").strip()
    if col:
        return col
    if not isinstance(payload, dict):
        return ""
    external = payload.get("external")
    if isinstance(external, dict):
        nested = str(external.get("queue_origin") or "").strip()
        if nested:
            return nested
    top = str(payload.get("queue_origin") or "").strip()
    if top:
        return top
    if payload.get("placeholder"):
        return "control_plane"
    return ""


def _resolve_external_correlation_id(payload: dict[str, Any] | None) -> str:
    """Service Bus / external correlation id from a job payload (external first)."""
    if not isinstance(payload, dict):
        return ""
    external = payload.get("external")
    if isinstance(external, dict):
        nested = str(external.get("external_correlation_id") or "").strip()
        if nested:
            return nested
    return str(payload.get("external_correlation_id") or "").strip()


def _local_to_blast_job(
    state: Any,
    split_children: dict[str, Any] | None = None,
    *,
    include_database_metadata: bool = False,
    refresh_blocked_reason: str | None = None,
    cluster_power_state: str | None = None,
) -> dict[str, Any]:
    payload = state.payload if isinstance(state.payload, dict) else {}
    progress = payload.get("_progress") if isinstance(payload.get("_progress"), dict) else None
    is_external_origin = isinstance(payload.get("external"), dict)
    # External-origin rows have NO local Celery task and no real per-phase
    # progress writes -- their authoritative step timeline is derived from the
    # embedded sibling snapshot. Reconcile / recovery paths (e.g. a transient
    # ``worker_lost`` flip that later flipped back to ``completed``) can leave
    # a stale single-entry ``_progress.steps`` payload behind that, if used,
    # collapses the timeline to just that recovery step. Ignore ``_progress``
    # on external rows so the projection always runs through the external
    # branch below. The same flip also leaves a stale ``error_code`` on the
    # row, cleared by the terminal-flip handling in ``_sync_external_jobs_to_table``.
    if is_external_origin:
        progress = None
    program = str(getattr(state, "program", None) or _payload_value(payload, "program") or "blast")
    db = str(getattr(state, "db", None) or _payload_value(payload, "db", "database") or "")
    if not db and is_external_origin:
        external_snapshot = payload["external"]
        db = str(
            external_snapshot.get("db_name")
            or external_snapshot.get("db")
            or ""
        )
    infrastructure = {
        "subscription_id": getattr(state, "subscription_id", None)
        or _payload_value(payload, "subscription_id"),
        "resource_group": getattr(state, "resource_group", None)
        or _payload_value(payload, "resource_group"),
        "region": _payload_value(payload, "region"),
        "storage_account": getattr(state, "storage_account", None)
        or _payload_value(payload, "storage_account"),
        "acr_name": _payload_value(payload, "acr_name"),
        "cluster_name": getattr(state, "cluster_name", None)
        or _payload_value(payload, "aks_cluster_name", "cluster_name"),
        # Canonical results prefix (#66/#67) so the SPA can show the real
        # results location (date-tiered when the layout flag is on) instead of
        # reconstructing a flat ``{job_id}/`` hint. Flat jobs surface
        # ``{job_id}/`` (matches the frontend fallback); omitted when unset.
        "results_prefix": getattr(state, "results_prefix", None),
    }
    # External-origin rows: when status is terminal-success the embedded
    # sibling snapshot is authoritative for ``error_code`` / ``error``. A
    # stale ``worker_lost`` left by an earlier false-positive reconcile must
    # not surface alongside ``status=completed``. The ``_sync_external_jobs_to_table``
    # terminal-flip handler clears the column on the row; this is the
    # defensive projection-side mirror so a row that has not yet been
    # rewritten still renders cleanly.
    response_error_code = state.error_code
    response_error = _job_error_for_response(state)
    if (
        is_external_origin
        and str(state.status or "").lower() in {"completed", "succeeded"}
    ):
        if response_error_code:
            response_error_code = ""
        response_error = None
    _row_submission_source = _resolve_local_submission_source(
        payload, is_external_origin, column=getattr(state, "submission_source", None)
    )
    _row_queue_origin = _resolve_local_queue_origin(
        payload, column=getattr(state, "queue_origin", None)
    )
    _row_correlation_id = (
        str(getattr(state, "external_correlation_id", "") or "")
        or _resolve_external_correlation_id(payload)
    )
    # config_snapshot for an external-origin stored row lives under
    # ``payload.external.config_snapshot`` (stamped by the drain); a local
    # dashboard job keeps it at the payload top level.
    _row_config_snapshot = payload.get("config_snapshot") if isinstance(payload, dict) else None
    _external_snapshot = payload.get("external") if isinstance(payload, dict) else None
    if (not _row_config_snapshot) and isinstance(_external_snapshot, dict):
        _ext_cfg = _external_snapshot.get("config_snapshot")
        if isinstance(_ext_cfg, dict) and _ext_cfg:
            _row_config_snapshot = _ext_cfg
    # Region: external-origin rows do not store it; resolve from the cluster
    # (1h cached, best-effort) so the detail shows it instead of "—".
    if is_external_origin and not str(infrastructure.get("region") or "").strip():
        try:
            from api.services.blast.external_config import resolve_cluster_region

            _region = resolve_cluster_region(
                str(infrastructure.get("subscription_id") or ""),
                str(infrastructure.get("resource_group") or ""),
                str(infrastructure.get("cluster_name") or ""),
            )
            if _region:
                infrastructure["region"] = _region
        except Exception:  # pragma: no cover - best-effort
            LOGGER.debug("local external job region resolve skipped", exc_info=True)
    out = {
        "job_id": state.job_id,
        "job_id_kind": "dashboard",
        "dashboard_job_id": state.job_id,
        "openapi_job_id": _payload_value(payload, "openapi_job_id"),
        "instance_id": state.task_id,
        "job_title": str(getattr(state, "job_title", None) or state.job_id),
        "program": program,
        "db": db,
        "status": state.status,
        "phase": state.phase or state.status,
        "task_id": state.task_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "error_code": response_error_code,
        "error": response_error,
        "payload": payload,
        "config_snapshot": _row_config_snapshot,
        "infrastructure": {k: v for k, v in infrastructure.items() if v not in (None, "")},
        "source": "dashboard" if _row_submission_source == "dashboard" else "external_api",
        "submission_source": _row_submission_source,
        "queue_origin": _row_queue_origin,
        "external_correlation_id": _row_correlation_id or None,
        "owner_upn": getattr(state, "owner_upn", None) or None,
    }
    out["target"] = build_target(
        resource_type="blast_job",
        job_id=str(state.job_id),
        job_id_kind="dashboard",
        dashboard_job_id=str(state.job_id),
        openapi_job_id=_payload_value(payload, "openapi_job_id"),
        links={
            "dashboard_status": f"/api/blast/jobs/{state.job_id}",
            "events": f"/api/blast/jobs/{state.job_id}/events",
            "results": f"/api/blast/jobs/{state.job_id}/results",
        },
    )
    if progress is not None:
        out["custom_status"] = progress
        out["output"] = {
            "status": state.status,
            "phase": state.phase or state.status,
            "steps": progress.get("steps", {}),
        }
        # Surface the terminal-failure hints the K8s refresh records in the
        # payload so the SPA banner resolves the correct failed step ("BLAST
        # Run", not the default "Submit Job") and shows the real cluster-side
        # error instead of the last benign helper log line.
        if isinstance(payload, dict):
            failed_step = payload.get("failed_step")
            if failed_step:
                out["output"]["failed_step"] = failed_step
            payload_error = payload.get("error")
            if payload_error:
                out["output"]["error"] = payload_error
    elif isinstance(payload, dict) and isinstance(payload.get("external"), dict):
        # External-origin rows (a `/v1/jobs` submit synced into our Table) never
        # carry a dashboard `_progress` — the embedded sibling snapshot lives at
        # payload['external']. Synthesize the SAME honest step timeline the
        # fresh-fetch projection produces so the Execution Steps section is not
        # blank (and dashboard-only warmup/staging steps are shown skipped, not
        # faked as completed). Drive it off the row's LIVE status (the embedded
        # snapshot status can be stale; repo.update only refreshes status/phase).
        from api.services.blast.external_job_projection import _external_step_projection

        external_snapshot = payload["external"]
        live_status = str(getattr(state, "status", "") or "")
        # For a failed external row, prefer the authoritative error recovered
        # from the sibling /jobs/{id} detail and persisted in the error_code
        # column by ``_sync_external_jobs_to_table`` over the generic "no error
        # detail" placeholder the bare /v1/jobs snapshot would otherwise yield.
        # ``response_error`` was computed above from error_code / payload.error
        # (and reset to None for terminal-success rows). Passing it as the
        # step projection's ``error_message`` flows the real cause into the
        # failed step's inline ``error`` / ``output`` too, not just the banner.
        failed_error_override = (
            response_error if live_status.lower() == "failed" else None
        )
        ext_projection = _external_step_projection(
            external_snapshot,
            dashboard_status=live_status,
            error_message=failed_error_override or None,
        )
        ext_steps = ext_projection["steps"]
        out["custom_status"] = {
            "phase": state.phase or live_status,
            "blast_status": str(external_snapshot.get("status") or live_status),
            "steps": ext_steps,
        }
        out["output"] = {
            "status": live_status,
            "phase": state.phase or live_status,
            "steps": ext_steps,
        }
        if ext_projection["error"]:
            out["output"]["error"] = ext_projection["error"]
            if not out.get("error"):
                out["error"] = ext_projection["error"]
        if ext_projection["failed_step"]:
            out["output"]["failed_step"] = ext_projection["failed_step"]
    if include_database_metadata:
        from api.services.blast.db_metadata import extract_trusted_storage_account

        # Jobs synced from the sibling OpenAPI store their blob-URL database
        # under payload.external.db and leave infrastructure.storage_account
        # empty. Recover the account (gated to the trusted workload account) so
        # the Storage-backed resolver fills the sequence/letter counts and
        # snapshot date, matching dashboard jobs. The trust gate stops an
        # attacker-influenced db URL from leaking the MI Storage token.
        external = payload.get("external") if isinstance(payload.get("external"), dict) else None
        storage_account = str(infrastructure.get("storage_account") or "")
        if not storage_account:
            storage_account = extract_trusted_storage_account(
                db
            ) or extract_trusted_storage_account(str((external or {}).get("db") or ""))
        database_metadata = _database_metadata_for_response(
            db,
            storage_account,
        )
        if database_metadata is not None:
            out["database_metadata"] = database_metadata
        # External-origin failed rows: the sibling only reports a coarse/empty
        # error, so recover the authoritative cluster-side blastn detail from
        # the results container (gated to this detail-view path). External jobs
        # store results under ``results/<openapi_job_id>/...``.
        if external is not None and isinstance(out.get("output"), dict):
            from api.services.blast.external_job_projection import (
                _enrich_external_failure_detail,
            )

            ext_output = out["output"]
            ext_steps = (
                ext_output.get("steps") if isinstance(ext_output.get("steps"), dict) else None
            )
            enriched_error = _enrich_external_failure_detail(
                status=str(out.get("status") or ""),
                current_error=ext_output.get("error") or out.get("error"),
                storage_account=storage_account,
                results_job_id=str(external.get("job_id") or ""),
                steps=ext_steps,
                failed_step=ext_output.get("failed_step"),
            )
            if enriched_error:
                ext_output["error"] = enriched_error
                out["error"] = enriched_error
    # Optional dashboard-friendly query name (used by the cluster bento
    # Active jobs cell to show "BRCA1 - chr17.fa" rather than the raw uuid).
    query_label = getattr(state, "query_label", None) or _payload_value(
        payload,
        "query_file",
        "query_name",
        "queries",
    )
    if query_label:
        out["query_label"] = str(query_label)[:120]
    if getattr(state, "parent_job_id", None):
        out["parent_job_id"] = state.parent_job_id
    if split_children is not None:
        out["split_children"] = split_children
        # Derived progress fields — pre-computed server-side so every
        # SPA consumer (cluster bento, BlastJobs page, modal) renders
        # the same numbers without each rolling its own count loop.
        counts = split_children.get("children_by_status") or {}
        if isinstance(counts, dict):
            done_states = {"completed", "succeeded", "success"}
            failed_states = {"failed", "error"}
            total = int(split_children.get("child_count") or 0)
            done = sum(int(v) for k, v in counts.items() if str(k).lower() in done_states)
            failed = sum(int(v) for k, v in counts.items() if str(k).lower() in failed_states)
            out["splits_done"] = done
            out["splits_failed"] = failed
            out["splits_total"] = total
    # Cluster-stopped / cluster-missing rows can't be refreshed against the
    # K8s API, so an active row would otherwise show a frozen "running" state
    # forever. Tag it as stale + surface the ARM power_state so the SPA can
    # render a "status frozen — cluster stopped" badge instead of a false
    # in-progress signal. Only meaningful for rows still in an active state.
    if refresh_blocked_reason and str(getattr(state, "status", "") or "").strip().casefold() in (
        "running",
        "submitted",
    ):
        out["stale"] = True
        out["refresh_blocked_reason"] = refresh_blocked_reason
        if cluster_power_state:
            out["cluster_power_state"] = cluster_power_state
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


def _scope_value_matches(actual: object, expected: str) -> bool:
    if not expected:
        return True
    if actual in (None, ""):
        return False
    return str(actual).casefold() == expected.casefold()


def _local_state_matches_job_scope(
    state: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> bool:
    """Re-check a row's scope after the OData query, with cluster precedence.

    Matches the storage-layer semantics in
    :meth:`JobStateRepository.list_for_scope`: when the caller asks for a
    specific ``cluster_name`` the row's ``resource_group`` is allowed to
    differ. The dashboard's workspace RG (where Storage / ACR live) and
    the cluster's own RG (typically ``rg-elb-cluster``) are different
    concepts; treating RG as a hard filter would silently drop jobs whose
    row was saved with the cluster RG. RG only acts as a hard filter when
    the caller did NOT pass a ``cluster_name``.
    """
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    sub_ok = _scope_value_matches(
        getattr(state, "subscription_id", None) or _payload_value(payload, "subscription_id"),
        subscription_id,
    )
    cluster_ok = _scope_value_matches(
        getattr(state, "cluster_name", None)
        or _payload_value(payload, "aks_cluster_name", "cluster_name"),
        cluster_name,
    )
    if cluster_name:
        return sub_ok and cluster_ok
    rg_ok = _scope_value_matches(
        getattr(state, "resource_group", None) or _payload_value(payload, "resource_group"),
        resource_group,
    )
    return sub_ok and rg_ok and cluster_ok


def _refresh_running_blast_state(repo: Any, state: Any) -> Any:
    if getattr(state, "type", "") != "blast" or getattr(state, "status", "") != "running":
        return state
    phase = str(getattr(state, "phase", "") or "").strip().casefold()
    if phase not in _K8S_REFRESH_PHASES:
        return state
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    # Prefer the indexed top-level columns so the refresh works for rows
    # returned by `list_for_owner(include_payload=False)` (the list endpoint
    # avoids the payload column to keep responses small).
    subscription_id = str(
        getattr(state, "subscription_id", None)
        or _payload_value(payload, "subscription_id")
        or ""
    )
    resource_group = str(
        getattr(state, "resource_group", None)
        or _payload_value(payload, "resource_group")
        or ""
    )
    cluster_name = str(
        getattr(state, "cluster_name", None)
        or _payload_value(payload, "cluster_name", "aks_cluster_name")
        or ""
    )
    storage_account = str(
        getattr(state, "storage_account", None) or _payload_value(payload, "storage_account") or ""
    )
    if not (subscription_id and resource_group and cluster_name):
        return state
    k8s_job_id = str(
        _payload_value(payload, "elastic_blast_job_id", "k8s_job_id")
        or _discover_elastic_blast_job_id(storage_account, str(state.job_id))
    )
    if not k8s_job_id:
        return state
    refresh_key = (str(state.job_id), subscription_id, resource_group, cluster_name)
    now = monotonic()
    last_check = _K8S_REFRESH_LAST_CHECK.get(refresh_key)
    if last_check is not None and now - last_check < _refresh_min_interval_seconds(phase):
        return state
    # Cluster-level negative cache: if this cluster's K8s API was recently
    # unreachable, skip the slow (~10 s/GET) refresh for every job on it until
    # the cooldown lapses, instead of re-paying the timeout once per job.
    cluster_key = (subscription_id, resource_group, cluster_name)
    cooldown_until = _K8S_REFRESH_CLUSTER_COOLDOWN.get(cluster_key)
    if cooldown_until is not None:
        if now < cooldown_until:
            return state
        # Cooldown lapsed — drop the stale entry so the map never retains
        # expired keys and a re-failure logs as a fresh outage episode.
        _K8S_REFRESH_CLUSTER_COOLDOWN.pop(cluster_key, None)
    try:
        from api.services import get_credential
        from api.services.monitoring import k8s_check_blast_status

        k8s = k8s_check_blast_status(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            namespace="default",
            job_id=k8s_job_id,
        )
    except Exception as exc:
        LOGGER.debug("blast k8s refresh skipped job_id=%s: %s", state.job_id, type(exc).__name__)
        _K8S_REFRESH_LAST_CHECK[refresh_key] = now
        _arm_cluster_refresh_cooldown(cluster_key, cluster_name, now, type(exc).__name__)
        return state
    k8s_status = str(k8s.get("status") or "")
    if k8s_status == "unknown":
        # `k8s_check_blast_status` maps an unreachable / errored cluster API
        # (timeout, TLS failure, non-200) to "unknown". Treat it as a cluster
        # outage and arm the cooldown so sibling jobs don't each re-pay the
        # multi-GET timeout.
        _K8S_REFRESH_LAST_CHECK[refresh_key] = now
        _arm_cluster_refresh_cooldown(cluster_key, cluster_name, now, "status=unknown")
        return state
    # Reachable with a concrete status → the cluster recovered (if it was ever
    # cooling down), so clear the negative cache for immediate live refreshes.
    _K8S_REFRESH_CLUSTER_COOLDOWN.pop(cluster_key, None)
    if k8s_status not in {"completed", "failed"}:
        _K8S_REFRESH_LAST_CHECK[refresh_key] = now
        return state
    _K8S_REFRESH_LAST_CHECK.pop(refresh_key, None)
    # We are about to rewrite `_progress` in the Table — if this row was
    # fetched without its payload (list endpoint uses include_payload=False),
    # pulling the full row first preserves the existing step history.
    state = _maybe_reload_with_payload(repo, state)
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    if k8s_status == "completed" and not _state_has_parseable_result_artifact(state, payload):
        try:
            updated = repo.update(
                state.job_id,
                status="running",
                phase="results_pending",
                payload=_payload_with_refresh_progress(
                    payload,
                    phase="results_pending",
                    status="running",
                    k8s=k8s,
                ),
            )
            repo.append_history(
                state.job_id,
                "k8s_completed_results_pending",
                {"status": "running", "phase": "results_pending", "k8s": k8s},
            )
            return updated
        except Exception as exc:
            LOGGER.debug("blast results-pending update skipped job_id=%s: %s", state.job_id, exc)
            return state
    try:
        if k8s_status == "failed":
            # Map the failure to the real execution step the row was on so the
            # dashboard says "BLAST Run failed" (not the default "Submit Job"),
            # and surface the cluster-side blastn diagnostics instead of the
            # last benign helper log line.
            failed_step_key = "exporting_results" if phase == "results_pending" else "running"
            error_detail = _read_blast_runtime_failure(storage_account, str(state.job_id)) or (
                f"BLAST job failed on the cluster ({int(k8s.get('failed') or 0)} pod(s) failed)."
            )
            updated = repo.update(
                state.job_id,
                status="failed",
                phase="failed",
                error_code="blast_search_failed",
                payload=_payload_with_refresh_progress(
                    payload,
                    phase="failed",
                    status="failed",
                    k8s=k8s,
                    failed_step_key=failed_step_key,
                    error_detail=error_detail,
                ),
            )
            repo.append_history(
                state.job_id,
                "k8s_status_refreshed",
                {
                    "status": "failed",
                    "phase": "failed",
                    "failed_step": failed_step_key,
                    "error": error_detail,
                    "k8s": k8s,
                },
            )
            return updated
        updated = repo.update(
            state.job_id,
            status=k8s_status,
            phase=k8s_status,
            payload=_payload_with_refresh_progress(
                payload,
                phase=k8s_status,
                status=k8s_status,
                k8s=k8s,
            ),
        )
        repo.append_history(
            state.job_id,
            "k8s_status_refreshed",
            {"status": k8s_status, "phase": k8s_status, "k8s": k8s},
        )
        return updated
    except Exception as exc:
        LOGGER.debug("blast k8s refresh update skipped job_id=%s: %s", state.job_id, exc)
        return state


def _row_refresh_scope(state: Any) -> tuple[str, str, str]:
    """Extract (subscription_id, resource_group, cluster_name) for a job row.

    Prefers the indexed top-level columns so it works for list rows fetched
    with ``include_payload=False``, falling back to the payload when present.
    """
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    subscription_id = str(
        getattr(state, "subscription_id", None) or _payload_value(payload, "subscription_id") or ""
    )
    resource_group = str(
        getattr(state, "resource_group", None) or _payload_value(payload, "resource_group") or ""
    )
    cluster_name = str(
        getattr(state, "cluster_name", None)
        or _payload_value(payload, "cluster_name", "aks_cluster_name")
        or ""
    )
    return subscription_id, resource_group, cluster_name


def _blocked_refresh_reasons(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Map ``job_id -> ClusterHealth`` for active rows whose AKS cluster is down.

    The list endpoint consults this so it can (a) SKIP the K8s refresh for a
    stopped/missing cluster — which would otherwise burn a ~10 s K8s API
    timeout per job — and (b) tag the affected active rows as ``stale`` so the
    SPA renders a "status frozen — cluster stopped" badge instead of a false
    "running" signal that never advances.

    Cost: one cached ARM ``ManagedClusters.get`` per distinct
    (sub, rg, cluster) via ``get_cluster_health`` (90 s TTL), so a fleet of
    stopped jobs costs one ARM call, not one per job. Returns ``{}`` when there
    are no active rows, no usable scope, or credentials are unavailable —
    the gate is best-effort and never blocks the list response.
    """
    active = [
        row
        for row in rows
        if str(getattr(row, "status", "") or "").strip().casefold() in ("running", "submitted")
        and str(getattr(row, "phase", "") or "").strip().casefold() in _K8S_REFRESH_PHASES
    ]
    if not active:
        return {}
    scopes: dict[tuple[str, str, str], list[str]] = {}
    for row in active:
        subscription_id, resource_group, cluster_name = _row_refresh_scope(row)
        if not (subscription_id and resource_group and cluster_name):
            continue
        scopes.setdefault((subscription_id, resource_group, cluster_name), []).append(
            str(row.job_id)
        )
    if not scopes:
        return {}
    try:
        from api.services import get_credential
        from api.services.cluster_health import get_cluster_health

        credential = get_credential()
    except Exception as exc:
        LOGGER.debug("blocked-refresh health gate skipped (no credential): %s", type(exc).__name__)
        return {}
    blocked: dict[str, dict[str, Any]] = {}
    for (subscription_id, resource_group, cluster_name), job_ids in scopes.items():
        try:
            health = get_cluster_health(
                credential, subscription_id, resource_group, cluster_name
            )
        except Exception as exc:
            LOGGER.debug(
                "blocked-refresh health probe skipped cluster=%s: %s",
                cluster_name,
                type(exc).__name__,
            )
            continue
        # `get_cluster_health` degrades open (healthy=True) when ARM is
        # unreachable, so we only block on a proven stopped/missing cluster.
        if health.get("healthy", True):
            continue
        for job_id in job_ids:
            blocked[job_id] = dict(health)
    return blocked


def _payload_with_refresh_progress(
    payload: dict[str, Any],
    *,
    phase: str,
    status: str,
    k8s: dict[str, Any],
    failed_step_key: str | None = None,
    error_detail: str = "",
) -> dict[str, Any]:
    out = dict(payload)
    elastic_blast_job_id = str(k8s.get("job_id") or "")
    if elastic_blast_job_id.startswith("job-"):
        out["elastic_blast_job_id"] = elastic_blast_job_id
    _raw_progress = out.get("_progress")
    progress = dict(_raw_progress) if isinstance(_raw_progress, dict) else {}
    _raw_steps = progress.get("steps")
    steps = dict(_raw_steps) if isinstance(_raw_steps, dict) else {}
    # On a K8s-stage failure the bare phase is ``failed`` — not a real timeline
    # step. Record the failure against the actual execution step (the search /
    # export step the row was on) so the dashboard says "BLAST Run failed",
    # not the default "Submit Job", and surfaces the real cluster-side error
    # instead of the last benign helper log line.
    if status == "failed":
        step_key = failed_step_key or "running"
    elif phase == "results_pending":
        step_key = "exporting_results"
    else:
        step_key = phase
    _raw_step = steps.get(step_key)
    step = dict(_raw_step) if isinstance(_raw_step, dict) else {}
    from datetime import UTC, datetime

    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    step.setdefault("started_at", str(step.get("updated_at") or updated_at))
    step.update({"phase": phase, "status": status, "updated_at": updated_at, "k8s": k8s})
    if status == "completed":
        step["success"] = True
        step.setdefault("completed_at", updated_at)
    elif status == "failed":
        step["success"] = False
        step.setdefault("completed_at", updated_at)
        if error_detail:
            step["error"] = error_detail
    steps[step_key] = step
    # Steps that ran before the terminal step succeeded (submit reached the
    # cluster), so mark any that are still ``running`` as completed — both on
    # success and on a later-stage failure — so the timeline stops spinning on
    # an earlier step.
    if step_key in _PROGRESS_STEP_ORDER:
        current_idx = _PROGRESS_STEP_ORDER.index(step_key)
        for previous_key in _PROGRESS_STEP_ORDER[:current_idx]:
            previous = steps.get(previous_key)
            if not isinstance(previous, dict) or previous.get("status") != "running":
                continue
            normalised = dict(previous)
            normalised.setdefault("started_at", str(previous.get("updated_at") or updated_at))
            normalised.update(
                {
                    "status": "completed",
                    "updated_at": updated_at,
                    "completed_at": updated_at,
                    "success": True,
                }
            )
            steps[previous_key] = normalised
    progress.update({"phase": phase, "status": status, "steps": steps})
    out["_progress"] = progress
    if status == "failed":
        out["failed_step"] = step_key
        if error_detail:
            out["error"] = error_detail
    return out


def _state_has_parseable_result_artifact(state: Any, payload: dict[str, Any]) -> bool:
    storage_account = str(
        getattr(state, "storage_account", None) or _payload_value(payload, "storage_account") or ""
    )
    if not storage_account:
        return False
    try:
        from api.services.blast.result_analytics import list_parseable_result_blobs

        return bool(list_parseable_result_blobs(storage_account, str(state.job_id)))
    except Exception as exc:
        LOGGER.info(
            "blast result artifact check unavailable job_id=%s: %s",
            getattr(state, "job_id", ""),
            type(exc).__name__,
        )
        return False


def _discover_elastic_blast_job_id(storage_account: str, job_id: str) -> str:
    if not storage_account or not job_id:
        return ""
    try:
        from api.services import get_credential
        from api.services.storage.data import _blob_service

        container = _blob_service(get_credential(), storage_account).get_container_client("results")
        from api.services.storage.job_prefix import (
            elastic_blast_subdir_prefix,
            resolve_results_prefix,
        )

        prefix = elastic_blast_subdir_prefix(resolve_results_prefix(job_id))
        for blob in container.list_blobs(name_starts_with=prefix):
            name = str(blob.name or "")
            parts = name.split("/", 2)
            if len(parts) >= 2 and parts[1].startswith("job-"):
                return parts[1]
    except Exception as exc:
        LOGGER.debug("elastic blast job id discovery skipped job_id=%s: %s", job_id, exc)
    return ""


def blast_shared_visibility_enabled() -> bool:
    """Return True when per-owner BLAST job isolation is relaxed.

    Development-stage switch. With ``BLAST_JOBS_SHARED_VISIBILITY=true`` every
    authenticated caller may list and open every job regardless of the row's
    ``owner_oid`` (the Recent searches page then shows all submitters' jobs).
    Default OFF preserves the production per-user privacy boundary; the route
    layer still requires ``require_caller`` either way. Flip this off before
    any multi-tenant / shared-subscription use.
    """
    return os.environ.get("BLAST_JOBS_SHARED_VISIBILITY", "").lower() == "true"


def _assert_job_owner(owner_oid: str | None, caller: CallerIdentity) -> None:
    """Raise ``403 not owner`` unless ``caller`` owns the job.

    No-ops when :func:`blast_shared_visibility_enabled` is on (dev stage) or
    the row carries no concrete ``owner_oid`` (cluster-shared / external rows).
    Centralises the per-route owner gate so the dev visibility switch has one
    authority instead of a dozen drifting inline comparisons.
    """
    if blast_shared_visibility_enabled():
        return
    if owner_oid and owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")


def _ensure_job_read_allowed(job_id: str, caller: CallerIdentity) -> None:
    """Authorise read access to a job for ``caller``.

    Fails CLOSED on Storage outage when MSAL auth is enforced — researchers
    would otherwise be able to read each other's jobs the moment the Table
    Storage owner index becomes unreachable. In dev-bypass mode the synthetic
    identity has no real OID and there is no multi-tenant isolation, so we
    fall through (researcher-on-their-own-laptop case).
    """
    dev_bypass = os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true"
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get(job_id)
    except Exception as exc:
        if dev_bypass:
            return
        LOGGER.warning("job authorisation lookup failed (failing closed): %s", type(exc).__name__)
        raise HTTPException(503, {"code": "auth_lookup_unavailable"}) from exc
    if state:
        _assert_job_owner(state.owner_oid, caller)


def _resolve_job_storage_account(job_id: str, supplied: str) -> str:
    """Reject a ``storage_account`` query parameter that does not match the
    JobState row for ``job_id``.

    This closes a confused-deputy gap on the job-bound BLAST routes
    (``/api/blast/jobs/{job_id}/...``): the caller passes
    ``storage_account=<x>`` as a query parameter, the api authenticates the
    request with the shared MI, and the MI very likely has Reader on a
    different storage account the caller chose. Without this gate a
    legitimate user could read result blobs from any storage account the
    MI can reach by lying about which account holds their job.

    Behaviour:

    * Authoritative record exists (JobState row carries a non-empty
      ``storage_account``) and ``supplied`` does not match → ``403
      cross_account_mismatch``. The error reveals only that a mismatch
      occurred; the recorded value is **not** echoed.
    * Authoritative record exists and matches → return the recorded
      value (used so downstream code is byte-identical regardless of
      caller-supplied casing).
    * Authoritative record does not record the storage account (legacy
      row written before the field landed, external sync row, or a
      submit-then-poll race before the row reaches Table Storage) → log
      a one-liner and return ``supplied`` unchanged. The fallback is
      intentional: a hard failure here would break the legacy job list.
    * ``AUTH_DEV_BYPASS=true`` and the lookup raises → return
      ``supplied`` (dev loop without a real state backend).
    """
    if not supplied:
        return supplied
    dev_bypass = os.environ.get("AUTH_DEV_BYPASS", "").lower() == "true"
    try:
        from api.services.state_repo import JobStateRepository

        state = JobStateRepository().get_summary(job_id)
    except Exception as exc:
        if dev_bypass:
            return supplied
        LOGGER.warning(
            "storage account cross-check lookup failed job_id=%s err=%s (failing closed)",
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(503, {"code": "auth_lookup_unavailable"}) from exc
    if state is None:
        # Job genuinely absent from Table Storage. This happens legitimately
        # right after submit (the row is being written) and on external sync
        # rows that have not been re-projected yet. Demoted to DEBUG: it fires
        # on every storage-backed result request for those rows and floods
        # App Insights without operator value (see issue #19). An operator
        # debugging a bogus job_id can re-enable with the api logger level.
        LOGGER.debug(
            "storage account cross-check: no JobState row for job_id=%s; "
            "accepting supplied value",
            job_id,
        )
        return supplied
    recorded = (getattr(state, "storage_account", "") or "").strip()
    if not recorded:
        LOGGER.debug(
            "storage account cross-check: JobState has no recorded account; "
            "accepting supplied value job_id=%s",
            job_id,
        )
        return supplied
    if recorded.lower() != supplied.strip().lower():
        # Do NOT echo the recorded value to the caller — that would leak
        # the correct account name to anyone probing job_ids.
        raise HTTPException(
            403,
            {
                "code": "cross_account_mismatch",
                "message": (
                    "supplied storage_account does not match the account "
                    "recorded when this job was submitted"
                ),
            },
        )
    return recorded
