"""Stubs for endpoints that have not yet been ported from the legacy
Function App. They return well-structured empty/202 responses so the SPA
renders without crashing while the real implementations land.

Each stub logs a `STUB_CALLED` warning so we can see in App Insights which
endpoints the SPA actually exercises in production and prioritise the real
implementations accordingly.
"""

from __future__ import annotations

import logging
import os
import re
import threading as _threading
import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from api.auth import CallerIdentity, require_caller
from api.services.aks_skus import DEFAULT_SKU, DEFAULT_SYSTEM_SKU, sku_list_response

LOGGER = logging.getLogger(__name__)

# Per-(account, db) lock registry for /api/blast/databases/{db}/shard.
# A shard daemon holds the lock for the lifetime of its background work
# so a re-clicked chip returns 409 instead of starting a second writer.
# The guard mutex protects insertions into the registry itself; the
# registry never shrinks (one entry per ever-touched DB) which is fine
# at the scale we operate at (low-tens of DBs per deployment).
_SHARD_LOCK_REGISTRY: dict[str, _threading.Lock] = {}
_SHARD_LOCK_REGISTRY_GUARD = _threading.Lock()
# Older than this and we treat a leftover sharding_in_progress=true
# marker as a crashed previous daemon (and allow re-trigger). Picked to
# be much larger than the worst-case wall time for ensure_shard_sets
# against the largest known DB while still small enough that a real
# crash recovers quickly.
_SHARD_STALE_SECONDS = 30 * 60
_WARMUP_RELEASE_BODY = Body(...)
_WARMUP_RELEASE_CALLER = Depends(require_caller)
_TAXONOMY_SEARCH_QUERY = Query(..., min_length=1, max_length=120)
_TAXONOMY_SEARCH_LIMIT = Query(default=10, ge=1, le=20)
_TAXONOMY_DETAIL_PATH = Path(..., ge=1, le=10_000_000_000)
_TAXONOMY_IMAGE_NAME = Query(..., min_length=1, max_length=120)
_TAXONOMY_TREE_PATH = Path(..., ge=1, le=10_000_000_000)
_TAXONOMY_TREE_SIBLING_LIMIT = Query(default=3, ge=1, le=8)

_BLAST_SUBMIT_OPTION_KEYS = frozenset(
    {
        "additional_options",
        "acr_name",
        "acr_resource_group",
        "allow_approximate_sharding",
        "batch_len",
        "db_auto_partition",
        "db_effective_search_space",
        "db_partitions",
        "db_partition_prefix",
        "db_sharded",
        "db_total_bytes",
        "db_total_letters",
        "disable_sharding",
        "enable_warmup",
        "evalue",
        "gap_extend",
        "gap_open",
        "is_inclusive",
        "low_complexity_filter",
        "machine_type",
        "max_target_seqs",
        "mem_limit",
        "mem_request",
        "num_nodes",
        "outfmt",
        "pd_size",
        "query_count",
        "query_effective_search_spaces",
        "reuse",
        "shard_sets",
        "sharding_mode",
        "taxid",
        "use_local_ssd",
        "word_size",
    }
)

_SEARCHSP_OPTION_RE = re.compile(r"(?<!\S)-searchsp(?:\s|=|$)")


def _stub_log(name: str, **ctx: Any) -> None:
    LOGGER.warning("STUB_CALLED endpoint=%s ctx=%s", name, ctx)


def _safe_delay(task, **kwargs):
    """Enqueue a Celery task, returning the AsyncResult. If the broker is
    unreachable (Redis down), raise 503 with a retryable hint instead of
    letting the OperationalError bubble as a 500."""
    try:
        return task.delay(**kwargs)
    except Exception as exc:
        if "Connection refused" in str(exc) or "OperationalError" in type(exc).__name__:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "broker_unavailable",
                    "message": (
                        "Task broker (Redis) is not reachable. The task cannot be queued right now."
                    ),
                    "retryable": True,
                    "retry_after_seconds": 30,
                },
            ) from exc
        raise


def _submit_options_from_body(body: dict[str, Any]) -> dict[str, Any]:
    raw_options = body.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    raw_searchsp = options.pop("searchsp", None)
    if raw_searchsp not in (None, "") and "db_effective_search_space" not in options:
        options["db_effective_search_space"] = raw_searchsp
    for key in _BLAST_SUBMIT_OPTION_KEYS:
        if key in body and body[key] not in (None, ""):
            options.setdefault(key, body[key])
    if "searchsp" in body and body["searchsp"] not in (None, ""):
        options.setdefault("db_effective_search_space", body["searchsp"])
    return options


def _apply_web_blast_searchsp_default(database: str, options: dict[str, Any]) -> None:
    from api.services.web_blast_searchsp import default_for_database

    default = default_for_database(database)
    if default is None:
        return

    if options.get("db_effective_search_space") not in (None, ""):
        pass
    elif options.get("query_effective_search_spaces") not in (None, ""):
        pass
    elif not _SEARCHSP_OPTION_RE.search(str(options.get("additional_options") or "")):
        options["db_effective_search_space"] = default.value

    # Web BLAST's nucleotide defaults use low-complexity filtering. Preserve an
    # explicit caller opt-out, but make browser and direct API submits converge.
    options.setdefault("low_complexity_filter", True)


def _upload_inline_query_for_submit(
    *,
    job_id: str,
    storage_account: str,
    query_data: str,
) -> tuple[str, dict[str, object]]:
    from api.services import get_credential
    from api.services.query_metadata import parse_fasta_metadata
    from api.services.storage_data import upload_query_text

    query_metadata = parse_fasta_metadata(query_data)
    blob_path = f"uploads/{job_id}/query.fa"
    try:
        upload_query_text(
            get_credential(),
            storage_account,
            "queries",
            blob_path,
            query_data,
        )
    except Exception as exc:
        raise HTTPException(
            503,
            detail={
                "code": "query_upload_failed",
                "message": f"Could not upload inline query FASTA: {type(exc).__name__}",
                "retryable": True,
            },
        ) from exc
    return f"queries/{blob_path}", query_metadata.as_dict()


def _normalise_blast_submit_body(body: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    normalised = dict(body)
    if not normalised.get("cluster_name") and normalised.get("aks_cluster_name"):
        normalised["cluster_name"] = normalised["aks_cluster_name"]
    if not normalised.get("database") and normalised.get("db"):
        normalised["database"] = normalised["db"]
    if not normalised.get("query_file") and normalised.get("query_blob_url"):
        normalised["query_file"] = normalised["query_blob_url"]

    options = _submit_options_from_body(normalised)
    _apply_web_blast_searchsp_default(
        str(normalised.get("database") or normalised.get("db") or ""),
        options,
    )
    query_data = normalised.get("query_data")
    if isinstance(query_data, str) and query_data.strip():
        try:
            from api.services.query_metadata import parse_fasta_metadata

            query_metadata = parse_fasta_metadata(query_data).as_dict()
        except Exception as exc:
            raise HTTPException(
                422,
                detail={"code": "invalid_query_fasta", "message": str(exc)[:500]},
            ) from exc
        options.setdefault("query_count", query_metadata.get("query_count"))
        if not normalised.get("query_file"):
            storage_account = str(normalised.get("storage_account") or "")
            if not storage_account:
                raise HTTPException(
                    422,
                    detail={
                        "code": "validation_error",
                        "message": "storage_account is required when query_data is submitted",
                    },
                )
            query_file, query_metadata = _upload_inline_query_for_submit(
                job_id=job_id,
                storage_account=storage_account,
                query_data=query_data,
            )
            normalised["query_file"] = query_file
        normalised["query_metadata"] = query_metadata
        normalised.pop("query_data", None)

    if options:
        normalised["options"] = options
    return normalised


def _safe_send_task(task_name: str, *, queue: str | None = None, **kwargs):
    """Enqueue a Celery task through the configured app, not shared_task current_app."""
    try:
        from api.celery_app import celery_app

        return celery_app.send_task(task_name, kwargs=kwargs, queue=queue)
    except Exception as exc:
        if "Connection refused" in str(exc) or "OperationalError" in type(exc).__name__:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "broker_unavailable",
                    "message": (
                        "Task broker (Redis) is not reachable. The task cannot be queued right now."
                    ),
                    "retryable": True,
                    "retry_after_seconds": 30,
                },
            ) from exc
        raise


def _payload_value(payload: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _external_status_to_dashboard(status: str) -> str:
    if status == "success":
        return "completed"
    if status in {"queued", "running", "failed", "cancelled"}:
        return status
    return "running" if status else "unknown"


def _exception_reason(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code")
            if code not in (None, ""):
                return str(code)
        if detail not in (None, ""):
            return str(detail)[:120]
        return f"http_{exc.status_code}"
    return type(exc).__name__


def _external_to_blast_job(job: dict[str, Any]) -> dict[str, Any]:
    external_status = str(job.get("status") or "unknown")
    status = _external_status_to_dashboard(external_status)
    db_name = str(job.get("db_name") or "")
    db = str(job.get("db") or db_name)
    program = str(job.get("program") or "blast")
    created_at = str(job.get("created_at") or "")
    updated_at = str(
        job.get("updated_at") or job.get("completed_at") or job.get("failed_at") or created_at
    )
    source = str(job.get("submission_source") or "external_api")
    job_title = f"{program} - {db_name or db}" if db_name or db else str(job.get("job_id") or "")
    out: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "job_title": job_title,
        "program": program,
        "db": db,
        "status": status,
        "phase": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "source": source,
        "submission_source": source,
        "external_correlation_id": job.get("external_correlation_id") or "",
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
    if job.get("cluster_name"):
        out["infrastructure"] = {"cluster_name": job.get("cluster_name")}
    if job.get("error"):
        error = job.get("error")
        out["error"] = error.get("message") if isinstance(error, dict) else str(error)
    return out


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


def _local_to_blast_job(state: Any, split_children: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = state.payload if isinstance(state.payload, dict) else {}
    program = str(_payload_value(payload, "program") or "blast")
    db = str(_payload_value(payload, "db", "database") or "")
    infrastructure = {
        "subscription_id": _payload_value(payload, "subscription_id"),
        "resource_group": _payload_value(payload, "resource_group"),
        "region": _payload_value(payload, "region"),
        "storage_account": _payload_value(payload, "storage_account"),
        "acr_name": _payload_value(payload, "acr_name"),
        "cluster_name": _payload_value(payload, "aks_cluster_name", "cluster_name"),
    }
    out = {
        "job_id": state.job_id,
        "instance_id": state.task_id,
        "job_title": str(
            _payload_value(payload, "job_title") or (f"{program} - {db}" if db else state.job_id)
        ),
        "program": program,
        "db": db,
        "status": state.status,
        "phase": state.phase or state.status,
        "task_id": state.task_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "error_code": state.error_code,
        "error": state.error_code,
        "payload": payload,
        "config_snapshot": payload.get("config_snapshot") if isinstance(payload, dict) else None,
        "infrastructure": {k: v for k, v in infrastructure.items() if v not in (None, "")},
        "source": "dashboard",
    }
    # Optional dashboard-friendly query name (used by the cluster bento
    # Active jobs cell to show "BRCA1 - chr17.fa" rather than the raw uuid).
    query_label = _payload_value(payload, "query_file", "query_name", "queries")
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
    return out


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
    if state and state.owner_oid and state.owner_oid != caller.object_id:
        raise HTTPException(403, "not owner")


# ===========================================================================
# /api/resources/* moved to api/routes/resources.py (real implementation).
# Keeping this empty router so the import in main.py keeps working without a
# code change at swap time. The real router takes precedence because it is
# included after this one.
# ===========================================================================
resources_router = APIRouter(prefix="/api/resources", tags=["resources-stub"])


# ===========================================================================
# /api/aks/* — provision, openapi/deploy/spec, skus, lifecycle
# ===========================================================================
aks_router = APIRouter(prefix="/api/aks", tags=["aks"])


@aks_router.get("/skus")
def aks_skus(
    location: str = Query(default="koreacentral"),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("aks/skus", location=location)
    # Source-of-truth lives in api.services.aks_skus, which mirrors the
    # sibling repo's elastic_blast.azure_traits.AZURE_HPC_MACHINES allow-list.
    # Picking anything outside this list makes elastic-blast raise
    # NotImplementedError("Cannot get properties for ...") at submit time, so
    # the SPA dropdown must source its options from here.
    #
    # `degraded` stays True until a Celery task replaces this with a live
    # Microsoft.Compute/skus query that intersects with the allow-list and
    # filters by region availability. The static list is correct for the
    # SKU set elastic-blast understands; what's missing is per-region
    # availability and quota.
    return sku_list_response()


@aks_router.post("/provision")
def aks_provision(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import provision_aks

    job_id = str(uuid.uuid4())
    result = _safe_delay(
        provision_aks,
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        region=body.get("region", "koreacentral"),
        cluster_name=body.get("cluster_name", "elb-cluster"),
        node_sku=body.get("node_sku", DEFAULT_SKU),
        node_count=body.get("node_count", 3),
        # Sibling repo's two-pool layout: small system pool + workload pool.
        # Defaults mirror constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE.
        system_vm_size=body.get("system_vm_size", DEFAULT_SYSTEM_SKU),
        system_node_count=body.get("system_node_count", 1),
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        storage_resource_group=body.get("storage_resource_group", ""),
        storage_account=body.get("storage_account", ""),
        caller_oid=caller.object_id,
    )
    return {
        "id": job_id,
        "job_id": job_id,
        "instance_id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@aks_router.post("/openapi/deploy")
def aks_openapi_deploy(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Re-deploy ``elb-openapi`` to an existing AKS cluster.

    Translates the SPA's ``OpenApiDeployPanel`` body into a Celery task
    enqueue. The returned ``id`` is the Celery task id so the SPA can poll
    ``GET /aks/openapi/deploy/{id}/status`` directly.
    """

    from api.tasks.openapi import deploy_openapi_service

    rg = body.get("resource_group", "") or ""
    cluster_name = body.get("cluster_name", "") or ""
    acr_name = body.get("acr_name", "") or ""
    if not (rg and cluster_name and acr_name):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_parameters",
                "message": (
                    "resource_group, cluster_name and acr_name are required "
                    "to deploy the OpenAPI service."
                ),
            },
        )

    result = _safe_delay(
        deploy_openapi_service,
        subscription_id=body.get("subscription_id", "") or "",
        resource_group=rg,
        cluster_name=cluster_name,
        acr_name=acr_name,
        storage_account=body.get("storage_account", "") or "",
        storage_resource_group=body.get("storage_resource_group", "") or "",
        tenant_id=caller.tenant_id or "",
        caller_oid=caller.object_id or "",
    )
    return {
        "id": result.id,
        "instance_id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/deploy/{result.id}/status",
        "status": "queued",
    }


@aks_router.get("/openapi/deploy/{instance_id}/status")
def aks_openapi_deploy_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Translate the Celery ``AsyncResult`` for a deploy_openapi task into
    the orchestrator-style envelope (``runtime_status`` + ``custom_status``
    + ``output``) the SPA's ``OpenApiDeployPanel`` was originally written
    against.
    """

    from celery.result import AsyncResult

    from api.celery_app import celery_app

    result = AsyncResult(instance_id, app=celery_app)
    status = (result.status or "PENDING").upper()
    runtime_status = {
        "PENDING": "Pending",
        "RECEIVED": "Pending",
        "STARTED": "Running",
        "RETRY": "Running",
        "PROGRESS": "Running",
        "SUCCESS": "Completed",
        "FAILURE": "Failed",
        "REVOKED": "Terminated",
    }.get(status, "Running")

    custom_status: dict[str, Any] = {"phase": status.lower()}
    output: dict[str, Any] | None = None

    if not result.ready():
        info = result.info if isinstance(result.info, dict) else None
        if info:
            custom_status.update({k: v for k, v in info.items() if k != "exc_type"})
    elif result.successful():
        payload = result.result if isinstance(result.result, dict) else {}
        succeeded = str(payload.get("status", "")).lower() == "succeeded"
        custom_status.update({"phase": "completed"})
        # The SPA reads ``output.openapi_deploy.error`` and
        # ``output.workload_identity.error`` to surface failures, so pass
        # the whole task payload through as ``output``.
        output = dict(payload)
        if not succeeded:
            output.setdefault("status", "failed")
    else:
        # FAILURE / REVOKED
        err = ""
        try:
            err = str(result.result or result.info or "")[:500]
        except Exception:
            err = "task failed"
        custom_status.update({"phase": "failed"})
        output = {
            "status": "failed",
            "openapi_deploy": {"error": err},
        }

    return {
        "instance_id": instance_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }


@aks_router.get("/openapi/spec")
def aks_openapi_spec(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Best-effort proxy for the deployed OpenAPI service's ``/openapi.json``.

    Resolves the LoadBalancer IP via the K8s API, then fetches the spec.
    Returns a degraded ``openapi:"3.0.0"`` placeholder when the service is
    not yet reachable so the SPA's docs page does not crash.
    """

    import httpx

    from api.services import get_credential
    from api.services.k8s_monitoring import k8s_get_service_ip

    sub = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", "")
    cred = get_credential()
    try:
        ip = k8s_get_service_ip(cred, sub, resource_group, cluster_name, "elb-openapi")
    except Exception as exc:
        ip = None
        LOGGER.warning("openapi/spec: k8s_get_service_ip failed: %s", exc)

    if not ip:
        return {
            "openapi": "3.0.0",
            "info": {"title": "elb-openapi (not yet deployed)", "version": "0.0.0"},
            "paths": {},
            "degraded": True,
            "degraded_reason": "openapi_service_not_reachable",
        }

    try:
        with httpx.Client(timeout=10.0) as client:
            for path in ("/openapi.json", "/docs/openapi.json"):
                resp = client.get(f"http://{ip}{path}")
                if resp.status_code == 200:
                    return resp.json()
    except Exception as exc:
        LOGGER.warning("openapi/spec: fetch failed for %s: %s", ip, exc)

    return {
        "openapi": "3.0.0",
        "info": {"title": "elb-openapi (spec not available)", "version": "0.0.0"},
        "paths": {},
        "degraded": True,
        "degraded_reason": "openapi_endpoint_unreachable",
    }


@aks_router.post("/start")
def aks_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import start_aks

    auto_warmup = body.get("auto_warmup") if isinstance(body.get("auto_warmup"), dict) else None
    result = _safe_delay(
        start_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
        auto_warmup=auto_warmup,
    )
    return {"task_id": result.id, "status": "queued"}


@aks_router.post("/stop")
def aks_stop(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import stop_aks

    result = _safe_delay(
        stop_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    return {"task_id": result.id, "status": "queued"}


@aks_router.post("/delete")
def aks_delete(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import delete_aks

    result = _safe_delay(
        delete_aks,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
    )
    return {"task_id": result.id, "status": "queued"}


@aks_router.post("/{cluster_name}/assign-roles")
def aks_assign_roles(
    cluster_name: str = Path(...),
    body: dict[str, Any] = Body(default={}),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.azure import assign_aks_roles

    result = _safe_delay(
        assign_aks_roles,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=cluster_name,
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        storage_resource_group=body.get("storage_resource_group", ""),
        storage_account=body.get("storage_account", ""),
    )
    return {"task_id": result.id, "status": "queued"}


# ===========================================================================
# /api/acr/* — ACR image build
# ===========================================================================
acr_build_router = APIRouter(prefix="/api/acr", tags=["acr"])


@acr_build_router.post("/build-images")
def acr_build_images(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.acr import build_images

    result = _safe_delay(
        build_images,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        registry_name=body.get("registry_name", ""),
        images=body.get("images"),
    )
    # For immediate feedback, return the expected images with "scheduled" status
    from api.services.image_tags import IMAGE_TAGS

    targets = body.get("images") or list(IMAGE_TAGS.keys())
    results = []
    for img in targets:
        tag = IMAGE_TAGS.get(img, "latest")
        results.append({"image": f"{img}:{tag}", "status": "scheduled"})
    return {"results": results, "task_id": result.id}


# ===========================================================================
# /api/blast/* — submit/jobs/databases/schedules
# ===========================================================================
blast_router = APIRouter(prefix="/api/blast", tags=["blast"])


@blast_router.get("/jobs")
def blast_jobs_list(
    limit: int = Query(default=50, ge=1, le=500),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List BLAST jobs from the platform table plus external OpenAPI jobs.

    Local Table-backed rows win when both sources know the same job. Direct
    OpenAPI submissions live in the sibling service's ConfigMaps, so merging
    them here keeps the SPA on one canonical jobs endpoint.
    """
    jobs: list[dict[str, Any]] = []
    degraded: dict[str, Any] = {}
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        rows = repo.list_for_owner(caller.object_id, limit=limit)
        parent_ids = [row.job_id for row in rows if row.type == "blast"]
        split_summaries = _split_child_summaries_from_repo(
            repo,
            caller.object_id,
            parent_ids,
        )
        for row in rows:
            if row.type != "blast":
                continue
            jobs.append(
                _local_to_blast_job(
                    row,
                    split_children=split_summaries.get(row.job_id),
                )
            )
    except Exception as exc:
        LOGGER.warning("blast_jobs_list failed: %s", type(exc).__name__)
        exc_name = type(exc).__name__
        if exc_name == "RuntimeError" and "AZURE_TABLE_ENDPOINT" in str(exc):
            degraded = {
                "degraded": True,
                "degraded_reason": "not_configured",
                "message": (
                    "Job state storage is not configured. Set AZURE_TABLE_ENDPOINT "
                    "to connect to Azure Table Storage."
                ),
            }
        else:
            degraded = {
                "degraded": True,
                "degraded_reason": "state_repo_unavailable",
                "message": f"Could not reach job state storage: {exc_name}",
            }

    external_degraded: dict[str, Any] = {}
    try:
        from api.services import external_blast

        external_rows = external_blast.list_jobs().get("jobs", [])
        if isinstance(external_rows, list):
            seen = {str(job.get("job_id")) for job in jobs}
            for row in external_rows:
                if not isinstance(row, dict):
                    continue
                job_id = str(row.get("job_id") or "")
                if not job_id or job_id in seen:
                    continue
                jobs.append(_external_to_blast_job(row))
                seen.add(job_id)
    except Exception as exc:
        LOGGER.info("external blast job list unavailable: %s", _exception_reason(exc))
        external_degraded = {
            "external_degraded": True,
            "external_degraded_reason": _exception_reason(exc),
        }

    jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    response: dict[str, Any] = {"jobs": jobs[:limit]}
    if degraded and not jobs:
        response.update(degraded)
    if external_degraded:
        response.update(external_degraded)
    return response


@blast_router.get("/jobs/{job_id}")
def blast_job_get(
    job_id: str = Path(...),
    history: int = Query(default=0),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    local_unavailable: Exception | None = None
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is not None:
            if state.owner_oid and state.owner_oid != caller.object_id:
                raise HTTPException(403, "not owner")
            out = _local_to_blast_job(
                state,
                split_children=_split_child_summary_from_repo(repo, state.job_id),
            )
            if history:
                out["history"] = repo.get_history(job_id, limit=200)
            return out
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_get failed: %s", type(exc).__name__)
        local_unavailable = exc

    try:
        from api.services import external_blast

        return _external_to_blast_job(external_blast.get_job(job_id))
    except HTTPException as exc:
        if exc.status_code == 404 and local_unavailable is not None:
            raise HTTPException(
                503,
                f"local job state unavailable: {type(local_unavailable).__name__}",
            ) from exc
        raise
    except Exception as exc:
        if local_unavailable is not None:
            raise HTTPException(
                503,
                "local job state unavailable: "
                f"{type(local_unavailable).__name__}; external lookup unavailable: "
                f"{type(exc).__name__}",
            ) from exc
        raise HTTPException(404, "job not found") from exc


@blast_router.post("/jobs/{job_id}/cancel")
def blast_job_cancel(
    job_id: str = Path(...),
    body: dict[str, Any] = Body(default={}),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.tasks.blast import cancel

    result = _safe_delay(
        cancel,
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        cluster_name=body.get("cluster_name", ""),
        storage_account=body.get("storage_account", ""),
    )
    return {"job_id": job_id, "task_id": result.id, "status": "cancelling"}


@blast_router.delete("/jobs/{job_id}")
def blast_job_delete(
    job_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Delete a job record from the state repository."""
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        state = repo.get(job_id)
        if state is None:
            raise HTTPException(404, "job not found")
        if state.owner_oid and state.owner_oid != caller.object_id:
            raise HTTPException(403, "not owner")
        repo.update(job_id, status="deleted", phase="deleted")
        return {"job_id": job_id, "status": "deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("blast_job_delete failed: %s", exc)
        return {"job_id": job_id, "status": "deleted"}


@blast_router.post("/pre-flight")
def blast_pre_flight(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run pre-flight checks before BLAST submit.

    Validates that the required infrastructure is in place:
    AKS cluster running, storage accessible, database exists, query valid.
    """
    checks: list[dict[str, Any]] = []
    critical = 0

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster = body.get("cluster_name") or body.get("aks_cluster_name") or ""
    storage = body.get("storage_account", "")
    db = body.get("db") or body.get("database", "")
    raw_options = body.get("options") if isinstance(body.get("options"), dict) else {}
    precision_options = {**raw_options}
    for key in (
        "additional_options",
        "allow_approximate_sharding",
        "db_auto_partition",
        "db_partitions",
        "db_partition_prefix",
        "db_effective_search_space",
        "db_total_letters",
        "outfmt",
        "query_effective_search_spaces",
        "searchsp",
        "sharding_mode",
    ):
        if key in body:
            if key == "searchsp":
                precision_options.setdefault("db_effective_search_space", body[key])
            else:
                precision_options[key] = body[key]
    _apply_web_blast_searchsp_default(str(db), precision_options)

    # 1. AKS cluster check
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters

        cred = get_credential()
        clusters = list_aks_clusters(cred, sub, rg)
        found = next((c for c in clusters if c.get("name") == cluster), None)
        if not found:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS Cluster",
                    "detail": f"Cluster '{cluster}' not found in '{rg}'",
                    "severity": "critical",
                }
            )
            critical += 1
        elif found.get("power_state") != "Running":
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS Cluster",
                    "detail": f"Cluster is {found.get('power_state', 'unknown')}. Start it first.",
                    "severity": "critical",
                    "action": "Start cluster",
                    "action_type": "start_cluster",
                }
            )
            critical += 1
        else:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "pass",
                    "title": "AKS Cluster",
                    "detail": f"{cluster} is running ({found.get('node_count', '?')} nodes)",
                }
            )
    except Exception as exc:
        checks.append(
            {
                "id": "aks_cluster",
                "status": "warn",
                "title": "AKS Cluster",
                "detail": f"Could not verify: {type(exc).__name__}",
            }
        )

    # 2. Storage check
    if storage:
        checks.append(
            {
                "id": "storage",
                "status": "pass",
                "title": "Storage Account",
                "detail": f"{storage} configured",
            }
        )
    else:
        checks.append(
            {
                "id": "storage",
                "status": "fail",
                "title": "Storage Account",
                "detail": "No storage account configured",
                "severity": "critical",
            }
        )
        critical += 1

    # 3. Database check
    if db:
        checks.append(
            {
                "id": "database",
                "status": "pass",
                "title": "BLAST Database",
                "detail": f"Database '{db}' selected",
            }
        )
    else:
        checks.append(
            {
                "id": "database",
                "status": "fail",
                "title": "BLAST Database",
                "detail": "No database selected",
                "severity": "critical",
            }
        )
        critical += 1

    # 3b. Sharding precision policy check
    try:
        from api.services.sharding_precision import build_precision_report

        query_metadata = None
        query_count = body.get("query_count")
        query_data = body.get("query_data")
        if isinstance(query_data, str) and query_data.strip():
            from api.services.query_metadata import parse_fasta_metadata

            query_metadata = parse_fasta_metadata(query_data)
            query_count = query_metadata.query_count
        elif not isinstance(query_count, int):
            query_count = None
        shard_sets = body.get("shard_sets")
        if not isinstance(shard_sets, list):
            shard_sets = None
        precision_report = build_precision_report(
            precision_options,
            query_count=query_count,
            db_stats_available=bool(precision_options.get("db_total_letters")),
            shard_sets=shard_sets,
        )
        status = "pass" if precision_report.eligible else "fail"
        checks.append(
            {
                "id": "sharding_precision",
                "status": status,
                "title": "Sharding Precision",
                "detail": precision_report.precision_level,
                "severity": "critical" if not precision_report.eligible else None,
                "precision": precision_report.as_dict(),
                "query_metadata": query_metadata.as_dict() if query_metadata else None,
            }
        )
        if not precision_report.eligible:
            critical += 1
    except Exception as exc:
        checks.append(
            {
                "id": "sharding_precision",
                "status": "fail",
                "title": "Sharding Precision",
                "detail": str(exc)[:200],
                "severity": "critical",
            }
        )
        critical += 1

    # 4. Redis/Celery broker check
    try:
        from api.celery_app import celery_app

        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.close()
        checks.append(
            {
                "id": "broker",
                "status": "pass",
                "title": "Task Broker",
                "detail": "Redis is reachable",
            }
        )
    except Exception:
        checks.append(
            {
                "id": "broker",
                "status": "fail",
                "title": "Task Broker",
                "detail": "Redis is not reachable. Tasks cannot be queued.",
                "severity": "critical",
            }
        )
        critical += 1

    ready = critical == 0
    return {
        "ready": ready,
        "checks": checks,
        "critical_blockers": critical,
        "summary": "All checks passed" if ready else f"{critical} critical issue(s) found",
    }


@blast_router.post("/submit")
def blast_submit(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    normalised_body = _normalise_blast_submit_body(body, job_id=job_id)

    # Input validation
    from api._http_utils import BlastSubmitRequest

    try:
        req = BlastSubmitRequest(**normalised_body)
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "validation_error", "message": str(exc)[:500]},
        ) from exc

    # Precision gate: exact/precise sharding claims must be validated before a
    # Celery task is queued. Approximate mode remains explicit and warning-only.
    try:
        from api.services.sharding_precision import build_precision_report, normalize_sharding_mode

        precision_options = dict(req.options or {})
        for key in ("query_count", "shard_sets"):
            if key in body and key not in precision_options:
                precision_options[key] = body[key]
        mode = normalize_sharding_mode(precision_options)
        if mode == "precise":
            report = build_precision_report(
                precision_options,
                query_count=precision_options.get("query_count"),
                db_stats_available=bool(precision_options.get("db_total_letters")),
                shard_sets=precision_options.get("shard_sets")
                if isinstance(precision_options.get("shard_sets"), list)
                else None,
            )
            if not report.eligible:
                raise HTTPException(
                    422,
                    detail={
                        "code": "sharding_precision_blocked",
                        "message": "; ".join(report.blocking_errors),
                        "precision": report.as_dict(),
                    },
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "sharding_precision_invalid", "message": str(exc)[:500]},
        ) from exc

    # Capacity pre-check — verify AKS cluster exists and is running
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters

        cred = get_credential()
        sub = req.subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        clusters = list_aks_clusters(cred, sub, req.resource_group)
        cluster = next((c for c in clusters if c.get("name") == req.cluster_name), None)
        if not cluster:
            raise HTTPException(
                409,
                detail={
                    "code": "cluster_not_found",
                    "message": (
                        f"AKS cluster '{req.cluster_name}' not found in '{req.resource_group}'"
                    ),
                    "retryable": False,
                },
            )
        power = cluster.get("power_state", "")
        if power != "Running":
            raise HTTPException(
                503,
                detail={
                    "code": "cluster_not_ready",
                    "message": f"AKS cluster '{req.cluster_name}' is {power}. Start it first.",
                    "retryable": True,
                    "retry_after_seconds": 60,
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("capacity pre-check failed (non-blocking): %s", exc)

    from api.tasks.blast import submit

    submit_options = dict(req.options or {})
    for key in ("acr_resource_group", "acr_name"):
        value = normalised_body.get(key)
        if value not in (None, ""):
            submit_options.setdefault(key, value)

    # Create job state record
    try:
        from datetime import datetime

        from api.services.state_repo import JobState, JobStateRepository

        now = datetime.now(UTC).isoformat(timespec="seconds")
        repo = JobStateRepository()
        state = JobState(
            job_id=job_id,
            type="blast",
            status="queued",
            phase="queued",
            owner_oid=caller.object_id,
            tenant_id=caller.tenant_id,
            created_at=now,
            updated_at=now,
            payload=normalised_body,
        )
        repo.create(state)
    except Exception as exc:
        LOGGER.warning("failed to create job state: %s", exc)

    result = _safe_delay(
        submit,
        job_id=job_id,
        subscription_id=req.subscription_id,
        resource_group=req.resource_group,
        cluster_name=req.cluster_name,
        storage_account=req.storage_account,
        program=req.program,
        database=req.database,
        query_file=req.query_file,
        options=submit_options,
        caller_oid=caller.object_id,
        caller_tenant_id=caller.tenant_id,
    )
    return {
        "id": job_id,
        "job_id": job_id,
        "task_id": result.id,
        "instance_id": result.id,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@blast_router.post("/jobs", status_code=202)
def blast_job_submit(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Canonical BLAST job submit endpoint.

    Dashboard submissions continue to use the local Celery path. Inline FASTA
    submissions use the sibling OpenAPI execution plane but are exposed under
    the same `/api/blast/jobs` domain so clients do not need a second jobs API.
    """
    if "query_fasta" not in body:
        return blast_submit(body, caller)

    from api.routes.elastic_blast import ExternalBlastSubmitRequest
    from api.services import external_blast

    try:
        request = ExternalBlastSubmitRequest(**body)
    except Exception as exc:
        raise HTTPException(
            422, detail={"code": "validation_error", "message": str(exc)[:500]}
        ) from exc

    payload = request.model_dump(exclude_none=True)
    payload["submission_source"] = "external_api"
    LOGGER.info(
        "canonical external BLAST submit accepted caller_oid=%s db=%s program=%s",
        caller.object_id,
        request.db,
        request.program,
    )
    return external_blast.submit_job(payload)


@blast_router.get("/submit/{instance_id}/status")
def blast_submit_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/submit/status", id=instance_id)
    return {
        "instance_id": instance_id,
        "runtime_status": "Pending",
        "custom_status": {"phase": "stub", "description": "Celery task pending"},
        "degraded": True,
        "degraded_reason": "celery_task_not_yet_implemented",
    }


@blast_router.post("/upload-query")
def blast_upload_query(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/upload-query", oid=caller.object_id)
    return {
        "status": "stub",
        "degraded": True,
        "degraded_reason": "streaming_proxy_not_yet_implemented",
    }


@blast_router.get("/databases")
def blast_databases(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    num_nodes: int = Query(default=0, ge=0, le=1000),
    machine_type: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    if not storage_account or not resource_group:
        return {"databases": []}
    from api.services import get_credential
    from api.services.storage_data import classify_storage_failure, list_databases
    from api.services.storage_public_access import ensure_local_storage_access

    cred = get_credential()
    if subscription_id:
        access = ensure_local_storage_access(cred, subscription_id, resource_group, storage_account)
        if access.get("action") == "failed":
            LOGGER.warning(
                "blast_databases: local-debug auto-open failed for %s: %s",
                storage_account,
                access.get("error"),
            )
    try:
        databases = list_databases(cred, storage_account)
    except Exception as exc:
        LOGGER.warning("blast_databases failed: %s", type(exc).__name__)
        return {
            "databases": [],
            **classify_storage_failure(cred, subscription_id, resource_group, storage_account, exc),
        }

    # Optional warmup plan enrichment. Only computed when the caller
    # supplied cluster topology — the planner needs node count + SKU and
    # the api sidecar deliberately does not re-query AKS here (an extra
    # ARM round trip per page render would be wasteful since the SPA
    # already loads /api/monitor/aks via useClusterReadiness).
    if num_nodes > 0 and machine_type:
        from api.services.warmup_planner import compute_warmup_feasibility

        for db in databases:
            try:
                plan = compute_warmup_feasibility(
                    db_total_bytes=int(db.get("total_bytes") or 0),
                    num_nodes=num_nodes,
                    machine_type=machine_type,
                )
                db["warmup_plan"] = plan.to_dict()
            except Exception as exc:  # planner only raises on programmer error
                LOGGER.warning(
                    "warmup_plan compute failed db=%s: %s",
                    db.get("name"),
                    type(exc).__name__,
                )
                # Honest degraded marker — never silently swallow.
                db["warmup_plan"] = {
                    "feasible": False,
                    "status": "no_db_size",
                    "message": "Warmup plan unavailable.",
                    "recommendations": [],
                }

    return {"databases": databases}


@blast_router.post("/databases/{db_name}/shard")
def blast_database_shard(
    db_name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run prepare-db's sharding step against an already-downloaded DB.

    **Async** — returns 202 immediately and runs ``ensure_shard_sets`` in
    a daemon thread (mirrors ``/api/storage/prepare-db``). Sharding for
    large DBs like ``core_nt`` does ~150+ small SDK round-trips and
    cannot complete inside an HTTP request window. Progress is published
    by writing ``sharding_in_progress`` / ``sharding_started_at`` /
    ``sharding_error`` into ``{db_name}-metadata.json`` so the SPA's
    ``GET /api/blast/databases`` poll renders the in-flight state
    (and survives a page reload).

    Hardening:
      * Per-``(account, db)`` lock prevents concurrent daemons from
        thrashing the metadata blob.
      * If a previous daemon's ``sharding_in_progress`` flag is older
        than ``_SHARD_STALE_SECONDS`` we treat it as crashed and allow
        re-trigger.
      * All error strings are passed through ``sanitise()`` before
        landing in the metadata blob or the response.
    """
    import json
    import re
    import threading
    from datetime import UTC, datetime

    from azure.core.exceptions import ResourceNotFoundError

    from api.services import get_credential
    from api.services.db_sharding import (
        DEFAULT_CONTAINER,
        ensure_shard_sets,
    )
    from api.services.sanitise import sanitise
    from api.services.storage_data import _blob_service  # type: ignore[attr-defined]
    from api.services.storage_public_access import ensure_local_storage_access

    sub = body.get("subscription_id", "")
    storage_rg = body.get("resource_group", "")
    account_name = body.get("account_name", "")
    if not all([sub, storage_rg, account_name]):
        raise HTTPException(
            400,
            "subscription_id, resource_group, account_name required in body",
        )
    # Mirror the validation in /api/storage/prepare-db. Keep it tight —
    # `db_name` flows straight to a blob path.
    _re_db = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
    _re_sub = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    _re_rg = re.compile(r"^[A-Za-z0-9._\-()]{1,90}$")
    _re_sa = re.compile(r"^[a-z0-9]{3,24}$")
    if not _re_db.match(db_name):
        raise HTTPException(400, "invalid db_name")
    if not _re_sub.match(sub):
        raise HTTPException(400, "invalid subscription_id")
    if not _re_rg.match(storage_rg):
        raise HTTPException(400, "invalid resource_group")
    if not _re_sa.match(account_name):
        raise HTTPException(400, "invalid account_name")

    cred = get_credential()
    # Local-debug auto-open mirrors /api/storage/prepare-db so this call
    # also works from a developer laptop. No-op inside the Container App.
    access = ensure_local_storage_access(cred, sub, storage_rg, account_name)
    if access.get("action") == "failed":
        LOGGER.warning(
            "blast_database_shard: local-debug auto-open failed for %s: %s",
            account_name,
            access.get("error"),
        )

    # Per-(account, db) lock — prevents the user double-clicking a chip
    # from spawning two daemons that race the metadata write. Lock is
    # acquired non-blocking; if it's already held we return 409 so the
    # SPA shows "already running" instead of starting a second writer.
    lock_key = f"{account_name.lower()}|{db_name}"
    with _SHARD_LOCK_REGISTRY_GUARD:
        lock = _SHARD_LOCK_REGISTRY.setdefault(lock_key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "sharding already in progress for this DB")

    # Read the current metadata so we can preserve unrelated fields
    # (source_version, downloaded_at, …) and detect a stale in-progress
    # marker from a crashed previous daemon.
    svc = _blob_service(cred, account_name)
    cc = svc.get_container_client(DEFAULT_CONTAINER)
    bc = cc.get_blob_client(f"{db_name}-metadata.json")
    existing: dict[str, Any] = {}
    try:
        existing = json.loads(bc.download_blob().readall().decode("utf-8"))
    except ResourceNotFoundError:
        existing = {"db_name": db_name}
    except Exception:
        existing = {"db_name": db_name}

    # Stale-flag recovery — if the previous daemon crashed the metadata
    # could be left with sharding_in_progress=true forever. Treat
    # markers older than _SHARD_STALE_SECONDS as crashed.
    if existing.get("sharding_in_progress"):
        started = existing.get("sharding_started_at") or ""
        try:
            started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            age = (datetime.now(UTC) - started_dt).total_seconds()
        except Exception:
            age = float("inf")  # parse failure → treat as stale
        if age < _SHARD_STALE_SECONDS:
            lock.release()
            raise HTTPException(409, "sharding already in progress for this DB")
        LOGGER.info(
            "blast_database_shard: clearing stale in-progress flag for %s (age=%.0fs)",
            db_name,
            age,
        )

    started_at = datetime.now(UTC).isoformat()
    existing["db_name"] = db_name
    existing["sharding_in_progress"] = True
    existing["sharding_started_at"] = started_at
    # Clear any prior error so the SPA doesn't keep showing a stale
    # failure once a fresh attempt is launched.
    existing.pop("sharding_error", None)
    try:
        bc.upload_blob(json.dumps(existing).encode("utf-8"), overwrite=True)
    except Exception as exc:
        lock.release()
        LOGGER.warning(
            "blast_database_shard: pre-state write failed db=%s: %s",
            db_name,
            type(exc).__name__,
        )
        raise HTTPException(502, f"metadata pre-write failed: {type(exc).__name__}") from exc

    LOGGER.info(
        "blast_database_shard accepted oid=%s db=%s account=%s",
        caller.object_id,
        db_name,
        account_name,
    )

    def _do_shard() -> None:
        """Background worker — owns the lock for the lifetime of the call."""
        from api.services import get_credential as _get_cred

        try:
            local_cred = _get_cred()
            summary = ensure_shard_sets(local_cred, account_name, db_name)
        except Exception as exc:
            LOGGER.warning(
                "blast_database_shard daemon failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
            err_msg = sanitise(f"{type(exc).__name__}: {exc}")[:300]
            try:
                local_cred = _get_cred()
                svc2 = _blob_service(local_cred, account_name)
                bc2 = svc2.get_container_client(DEFAULT_CONTAINER).get_blob_client(
                    f"{db_name}-metadata.json"
                )
                final: dict[str, Any] = {}
                try:
                    final = json.loads(bc2.download_blob().readall().decode("utf-8"))
                except Exception:
                    final = {"db_name": db_name}
                final["sharding_in_progress"] = False
                final["sharding_error"] = err_msg
                bc2.upload_blob(json.dumps(final).encode("utf-8"), overwrite=True)
            except Exception as inner:
                LOGGER.warning(
                    "blast_database_shard error-state write failed db=%s: %s",
                    db_name,
                    type(inner).__name__,
                )
            finally:
                lock.release()
            return

        # Success — merge the summary into metadata.
        try:
            local_cred = _get_cred()
            svc2 = _blob_service(local_cred, account_name)
            bc2 = svc2.get_container_client(DEFAULT_CONTAINER).get_blob_client(
                f"{db_name}-metadata.json"
            )
            final: dict[str, Any] = {}
            try:
                final = json.loads(bc2.download_blob().readall().decode("utf-8"))
            except Exception:
                final = {"db_name": db_name}
            final["sharding_in_progress"] = False
            final.pop("sharding_error", None)
            final["sharded"] = bool(summary.get("shard_sets"))
            final["shard_sets"] = summary.get("shard_sets", [])
            final["sharded_at"] = datetime.now(UTC).isoformat()
            if summary.get("total_bytes"):
                final.setdefault("total_bytes", summary["total_bytes"])
            for key in ("total_letters", "total_sequences", "bytes_to_cache", "bytes_total"):
                if summary.get(key):
                    final.setdefault(key, summary[key])
            bc2.upload_blob(json.dumps(final).encode("utf-8"), overwrite=True)
            LOGGER.info(
                "blast_database_shard daemon ok db=%s shard_sets=%s",
                db_name,
                summary.get("shard_sets"),
            )
        except Exception as exc:
            LOGGER.warning(
                "blast_database_shard final-state write failed db=%s: %s",
                db_name,
                type(exc).__name__,
            )
        finally:
            lock.release()

    threading.Thread(
        target=_do_shard,
        daemon=True,
        name=f"shard-{db_name}",
    ).start()

    return {
        "accepted": True,
        "db_name": db_name,
        "sharding_started_at": started_at,
        "output": (
            "Sharding started in background. Poll /api/blast/databases for "
            "progress (look at sharding_in_progress / sharded / shard_sets)."
        ),
    }


@blast_router.get("/databases/check-updates")
def blast_databases_check_updates(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return NCBI's current ``latest-dir`` snapshot id.

    The SPA compares this against each downloaded DB's ``source_version``
    (written into ``{db}-metadata.json`` by ``/api/storage/prepare-db``) to
    flag DBs whose snapshot is stale. The lookup is a single unauthenticated
    GET to the NCBI public S3 bucket — fast and cheap; it is intentionally
    not Celery-backed.
    """
    try:
        import httpx

        resp = httpx.get(
            "https://ncbi-blast-databases.s3.amazonaws.com/latest-dir",
            timeout=15.0,
        )
        resp.raise_for_status()
        return {
            "latest_version": resp.text.strip(),
            "updates_available": [],
        }
    except Exception as exc:
        LOGGER.warning("blast/databases/check-updates failed: %s", type(exc).__name__)
        return {
            "latest_version": "",
            "updates_available": [],
            "degraded": True,
            "degraded_reason": "ncbi_unreachable",
            "message": f"Could not contact NCBI: {type(exc).__name__}",
        }


@blast_router.get("/databases/versions")
def blast_databases_versions(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/databases/versions", sa=storage_account)
    return {
        "versions": {},
        "degraded": True,
        "degraded_reason": "blast_db_listing_not_yet_implemented",
    }


# --- Lab Tools: pre-flight estimators and sidecar-dependent utilities ---
#
# These endpoints are referenced by the SPA (`web/src/api/endpoints.ts`,
# `web/src/pages/tools/ToolTabs.tsx`, `web/src/pages/DatabaseBuilder.tsx`)
# but their Celery tasks have not been ported from the legacy Function App
# yet. Returning a structured 503 here turns silent 404s into a clear
# "backend pending" signal the UI can render.

_LAB_TOOL_PENDING = {
    "code": "lab_tool_backend_pending",
    "message": "This Lab Tool route has no backend implementation in the Container Apps build yet.",
}


@blast_router.post("/cost-estimate")
def blast_cost_estimate_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/cost-estimate")
    raise HTTPException(503, detail=_LAB_TOOL_PENDING)


@blast_router.post("/preprocess")
def blast_preprocess_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/preprocess")
    raise HTTPException(503, detail=_LAB_TOOL_PENDING)


@blast_router.post("/taxonomy")
def blast_taxonomy_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/taxonomy")
    raise HTTPException(503, detail=_LAB_TOOL_PENDING)


@blast_router.get("/taxonomy/search")
def blast_taxonomy_search(
    q: str = _TAXONOMY_SEARCH_QUERY,
    limit: int = _TAXONOMY_SEARCH_LIMIT,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy import TaxonomySearchUnavailable, search_taxonomy

    del caller
    try:
        return search_taxonomy(q, limit=limit)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_query_invalid", "message": str(exc)},
        ) from exc
    except TaxonomySearchUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "taxonomy_lookup_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc


@blast_router.get("/taxonomy/detail/{taxid}")
def blast_taxonomy_detail(
    taxid: int = _TAXONOMY_DETAIL_PATH,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy import TaxonomySearchUnavailable, fetch_taxonomy_detail

    del caller
    try:
        return fetch_taxonomy_detail(taxid)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_taxid_invalid", "message": str(exc)},
        ) from exc
    except TaxonomySearchUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "taxonomy_lookup_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc


@blast_router.get("/taxonomy/image")
def blast_taxonomy_image(
    name: str = _TAXONOMY_IMAGE_NAME,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy_image import (
        TaxonomyImageUnavailable,
        fetch_taxonomy_image,
    )

    del caller
    try:
        return fetch_taxonomy_image(name)
    except TaxonomyImageUnavailable as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_image_invalid_name", "message": str(exc)},
        ) from exc


@blast_router.get("/taxonomy/tree/{taxid}")
def blast_taxonomy_tree(
    taxid: int = _TAXONOMY_TREE_PATH,
    sibling_limit: int = _TAXONOMY_TREE_SIBLING_LIMIT,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    from api.services.taxonomy import TaxonomySearchUnavailable, fetch_taxonomy_tree

    del caller
    try:
        return fetch_taxonomy_tree(taxid, sibling_limit=sibling_limit)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "taxonomy_taxid_invalid", "message": str(exc)},
        ) from exc
    except TaxonomySearchUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "taxonomy_tree_unavailable",
                "message": str(exc),
                "retryable": True,
                "retry_after_seconds": 30,
            },
        ) from exc


@blast_router.post("/primer-design")
def blast_primer_design_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/primer-design")
    raise HTTPException(503, detail=_LAB_TOOL_PENDING)


@blast_router.post("/databases/build")
def blast_databases_build_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/databases/build")
    raise HTTPException(503, detail=_LAB_TOOL_PENDING)


# --- Schedules ---
@blast_router.get("/schedules")
def blast_schedules_list(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/list")
    return {
        "schedules": [],
        "degraded": True,
        "degraded_reason": "beat_scheduler_not_yet_implemented",
    }


@blast_router.post("/schedules")
def blast_schedules_create(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/create", body_keys=list(body.keys()))
    raise HTTPException(
        503,
        detail={
            "code": "celery_beat_pending",
            "message": "Beat-driven schedules not yet implemented in the Container Apps backend.",
        },
    )


@blast_router.delete("/schedules/{schedule_id}")
def blast_schedules_delete(
    schedule_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/delete", id=schedule_id)
    raise HTTPException(503, detail={"code": "celery_beat_pending"})


@blast_router.post("/schedules/{schedule_id}/run")
def blast_schedules_run(
    schedule_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/schedules/run", id=schedule_id)
    raise HTTPException(503, detail={"code": "celery_beat_pending"})


# --- Result download / aggregate / export ---
@blast_router.get("/jobs/{job_id}/file")
def blast_job_file(
    job_id: str = Path(...),
    name: str = Query(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    max_bytes: int = Query(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Read a result file from storage (streamed through the api sidecar)."""
    try:
        from api.services import get_credential
        from api.services.storage_data import read_blob_text

        cred = get_credential()
        blob_path = f"{job_id}/{name}" if not name.startswith(job_id) else name
        content = read_blob_text(
            cred,
            storage_account,
            container="results",
            blob_path=blob_path,
            max_bytes=max_bytes,
        )
        return {
            "job_id": job_id,
            "name": name,
            "content": content,
            "truncated": len(content) >= max_bytes,
        }
    except Exception as exc:
        LOGGER.warning("blast_job_file failed: %s", type(exc).__name__)
        from api.services import get_credential as _get_cred
        from api.services.storage_data import classify_storage_failure

        info = classify_storage_failure(_get_cred(), subscription_id, "", storage_account, exc)
        raise HTTPException(
            404 if info["degraded_reason"] == "not_found" else 503,
            detail={"code": info["degraded_reason"], "message": info["message"]},
        ) from exc


@blast_router.get("/jobs/{job_id}/results")
def blast_job_results(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List result blobs for a BLAST job from storage."""
    _ensure_job_read_allowed(job_id, caller)
    local_failure: dict[str, Any] | None = None
    try:
        if storage_account:
            from api.services import get_credential
            from api.services.storage_data import list_result_blobs

            cred = get_credential()
            files = list_result_blobs(cred, storage_account, container="results", prefix=job_id)
            return {"job_id": job_id, "files": files, "results": files}
    except Exception as exc:
        LOGGER.warning("blast_job_results failed: %s", type(exc).__name__)
        from api.services import get_credential as _get_cred
        from api.services.storage_data import classify_storage_failure

        local_failure = classify_storage_failure(
            _get_cred(), subscription_id, resource_group, storage_account, exc
        )

    try:
        from api.services import external_blast

        files = _external_result_files(external_blast.get_job(job_id))
        if files:
            return {"job_id": job_id, "files": files, "results": files, "source": "external"}
    except Exception as exc:
        LOGGER.info("external blast result list unavailable: %s", type(exc).__name__)

    if local_failure:
        return {"job_id": job_id, "files": [], "results": [], **local_failure}
    return {"job_id": job_id, "files": [], "results": []}


# Caps applied when parsing result blobs in the request thread. Real BLAST
# tabular files are kilobytes-to-low-megabytes; these caps prevent a
# pathologically large `-outfmt 7` from blowing the api sidecar memory if a
# user accidentally produced one with `-max_target_seqs 100000`.
_RESULTS_MAX_FILES = 20
_RESULTS_AGGREGATE_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB / file
_RESULTS_ALIGNMENTS_MAX_BYTES = 20 * 1024 * 1024  # 20 MiB / file (single file)
_RESULTS_EXPORT_MAX_BYTES = 10 * 1024 * 1024


def _list_result_out_blobs(storage_account: str, job_id: str) -> list[dict[str, Any]]:
    """Return the `.out` (BLAST tabular) blobs that belong to a job."""
    from api.services import get_credential
    from api.services.storage_data import list_result_blobs

    cred = get_credential()
    blobs = list_result_blobs(cred, storage_account, container="results", prefix=f"{job_id}/")
    return [b for b in blobs if isinstance(b.get("name"), str) and b["name"].endswith(".out")]


def _validate_result_blob_name(blob_name: str, job_id: str) -> None:
    """Raise 400 if the supplied blob name escapes the job's result prefix."""
    if not job_id or "/" in job_id or ".." in job_id:
        raise HTTPException(400, detail={"code": "invalid_job_id"})
    if not blob_name.startswith(f"{job_id}/"):
        raise HTTPException(
            400,
            detail={"code": "invalid_blob_name", "message": "blob does not belong to this job"},
        )
    # Block path-traversal / URL-encoding tricks. Backslashes are treated as
    # separators by some Azure SDK layers, so reject them too.
    if ".." in blob_name or "?" in blob_name or "#" in blob_name or "\\" in blob_name:
        raise HTTPException(400, detail={"code": "invalid_blob_name"})
    if "%2e" in blob_name.lower() or "%2f" in blob_name.lower():
        raise HTTPException(400, detail={"code": "invalid_blob_name"})
    # Reject leading slash in the part after the prefix (defence in depth).
    remainder = blob_name[len(job_id) + 1 :]
    if remainder.startswith("/") or remainder == "":
        raise HTTPException(400, detail={"code": "invalid_blob_name"})


@blast_router.get("/jobs/{job_id}/results/aggregate")
def blast_job_results_aggregate(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Parse `.out` result blobs and return aggregate statistics for analytics."""
    _ensure_job_read_allowed(job_id, caller)
    from api.services import get_credential
    from api.services.blast_results_parser import aggregate_blast_hits, parse_blast_tabular
    from api.services.storage_data import read_blob_text

    try:
        out_blobs = _list_result_out_blobs(storage_account, job_id)
    except Exception as exc:
        LOGGER.warning("results aggregate: list_result_blobs failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "storage_unreachable",
            "stats": None,
        }

    if not out_blobs:
        return {
            "job_id": job_id,
            "status": "no_results",
            "message": "No .out result files found for this job.",
            "stats": None,
            "files_parsed": 0,
            "total_files": 0,
        }

    cred = get_credential()
    all_hits: list[dict[str, Any]] = []
    parsed_files = 0
    read_failures = 0
    for blob_info in out_blobs[:_RESULTS_MAX_FILES]:
        try:
            content = read_blob_text(
                cred,
                storage_account,
                "results",
                blob_info["name"],
                max_bytes=_RESULTS_AGGREGATE_MAX_BYTES,
            )
            all_hits.extend(parse_blast_tabular(content))
            parsed_files += 1
        except Exception as exc:
            read_failures += 1
            LOGGER.warning(
                "results aggregate: failed to parse %s: %s", blob_info["name"], type(exc).__name__
            )

    # If every blob read failed, surface that as a storage degradation rather
    # than "no hits" — a researcher staring at an empty analytics card needs
    # to know it's an infra issue, not a biology one.
    if parsed_files == 0 and read_failures > 0:
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "all_reads_failed",
            "message": (
                f"Failed to read any of {read_failures} result file(s). "
                "Storage may be unreachable or RBAC missing."
            ),
            "stats": None,
            "files_parsed": 0,
            "total_files": len(out_blobs),
            "read_failures": read_failures,
        }

    if not all_hits:
        return {
            "job_id": job_id,
            "status": "no_hits",
            "message": "No BLAST hits found in result files.",
            "stats": {"total_hits": 0},
            "files_parsed": parsed_files,
            "total_files": len(out_blobs),
            "read_failures": read_failures,
            "truncated": len(out_blobs) > _RESULTS_MAX_FILES,
        }

    try:
        stats = aggregate_blast_hits(all_hits)
    except Exception as exc:
        # Defensive: aggregate_blast_hits is pure-Python and well-tested,
        # but if an unexpected hit shape sneaks through (e.g. NaN in evalue),
        # report it as degraded rather than 500.
        LOGGER.warning("results aggregate: stats failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "aggregation_failed",
            "stats": None,
            "files_parsed": parsed_files,
            "total_files": len(out_blobs),
            "read_failures": read_failures,
        }
    return {
        "job_id": job_id,
        "status": "ok",
        "stats": stats,
        "files_parsed": parsed_files,
        "total_files": len(out_blobs),
        "read_failures": read_failures,
        "truncated": len(out_blobs) > _RESULTS_MAX_FILES,
    }


@blast_router.get("/jobs/{job_id}/results/alignments")
def blast_job_results_alignments(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    blob_name: str = Query(default=""),
    max_alignments: int = Query(default=50, ge=1, le=500),
    query_id: str = Query(default=""),
    min_identity: float = Query(default=0.0, ge=0.0, le=100.0),
    min_bitscore: float = Query(default=0.0, ge=0.0),
    max_evalue: float = Query(default=10.0, ge=0.0),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return parsed alignments from a `.out` file, optionally filtered.

    `min_identity` / `min_bitscore` / `max_evalue` let the SPA narrow down a
    large hit table without re-downloading the file. `query_id` returns hits
    for a single query — useful when reviewing a primer pair or amplicon
    individually.
    """
    _ensure_job_read_allowed(job_id, caller)
    from api.services import get_credential
    from api.services.blast_results_parser import parse_blast_tabular
    from api.services.storage_data import read_blob_text

    target_blob = blob_name.strip()
    try:
        if not target_blob:
            out_blobs = _list_result_out_blobs(storage_account, job_id)
            if not out_blobs:
                return {
                    "job_id": job_id,
                    "alignments": [],
                    "message": "No result files",
                    "total_hits": 0,
                    "returned": 0,
                    "query_ids": [],
                }
            target_blob = out_blobs[0]["name"]
        else:
            _validate_result_blob_name(target_blob, job_id)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("results alignments: list failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "alignments": [],
            "degraded": True,
            "degraded_reason": "storage_unreachable",
            "total_hits": 0,
            "returned": 0,
            "query_ids": [],
        }

    cred = get_credential()
    try:
        content = read_blob_text(
            cred,
            storage_account,
            "results",
            target_blob,
            max_bytes=_RESULTS_ALIGNMENTS_MAX_BYTES,
        )
    except Exception as exc:
        LOGGER.warning("results alignments: read failed: %s", type(exc).__name__)
        return {
            "job_id": job_id,
            "alignments": [],
            "degraded": True,
            "degraded_reason": "storage_unreachable",
            "total_hits": 0,
            "returned": 0,
            "query_ids": [],
        }

    all_hits = parse_blast_tabular(content)
    query_ids = sorted({str(h.get("qseqid", "")) for h in all_hits if h.get("qseqid")})

    filtered: list[dict[str, Any]] = []
    qid_filter = query_id.strip()
    for hit in all_hits:
        if qid_filter and hit.get("qseqid") != qid_filter:
            continue
        pident = hit.get("pident")
        if isinstance(pident, (int, float)) and pident < min_identity:
            continue
        bitscore = hit.get("bitscore")
        if isinstance(bitscore, (int, float)) and bitscore < min_bitscore:
            continue
        evalue = hit.get("evalue")
        if isinstance(evalue, (int, float)) and evalue > max_evalue:
            continue
        filtered.append(hit)

    return {
        "job_id": job_id,
        "blob_name": target_blob,
        "alignments": filtered[:max_alignments],
        "total_hits": len(all_hits),
        "filtered_hits": len(filtered),
        "returned": min(len(filtered), max_alignments),
        "query_ids": query_ids[:200],
        "filters": {
            "query_id": qid_filter or None,
            "min_identity": min_identity,
            "min_bitscore": min_bitscore,
            "max_evalue": max_evalue,
        },
    }


@blast_router.get("/jobs/{job_id}/results/download")
def blast_job_results_download(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    blob_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Stream a single result blob through the api sidecar."""
    _ensure_job_read_allowed(job_id, caller)
    _validate_result_blob_name(blob_name, job_id)
    from api.services import get_credential
    from api.services.storage_data import (
        result_media_type,
        safe_download_filename,
        stream_blob_bytes,
    )

    filename = safe_download_filename(blob_name)
    return StreamingResponse(
        stream_blob_bytes(get_credential(), storage_account, "results", blob_name),
        media_type=result_media_type(filename),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@blast_router.get("/jobs/{job_id}/results/export")
def blast_job_results_export(
    job_id: str = Path(...),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(...),
    format: str = Query(default="csv", pattern=r"^(csv|tsv|json)$"),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Export all parsed hits for a job as CSV / TSV / JSON.

    Researchers paste the CSV into Excel / R / Python for downstream
    analysis, so the column set matches BLAST `-outfmt 6` plus the extras
    captured from `# Fields:` headers when available.
    """
    _ensure_job_read_allowed(job_id, caller)
    import csv
    import io
    import json

    from api.services import get_credential
    from api.services.blast_results_parser import (
        EXPORT_DEFAULT_COLUMNS,
        EXPORT_EXTRA_COLUMNS,
        parse_blast_tabular,
    )
    from api.services.storage_data import read_blob_text

    try:
        out_blobs = _list_result_out_blobs(storage_account, job_id)
    except Exception as exc:
        LOGGER.warning("results export: list_result_blobs failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            detail={"code": "storage_unreachable", "message": "Could not list result blobs."},
        ) from exc

    cred = get_credential()
    all_hits: list[dict[str, Any]] = []
    read_failures = 0
    for blob_info in out_blobs[:_RESULTS_MAX_FILES]:
        try:
            content = read_blob_text(
                cred,
                storage_account,
                "results",
                blob_info["name"],
                max_bytes=_RESULTS_EXPORT_MAX_BYTES,
            )
            all_hits.extend(parse_blast_tabular(content))
        except Exception:
            read_failures += 1
            LOGGER.debug("results export: failed to parse blob", exc_info=True)

    # If we had blobs to read but every read failed, the export would otherwise
    # be a misleading header-only CSV. Fail loudly instead.
    if out_blobs and read_failures == len(out_blobs[:_RESULTS_MAX_FILES]):
        raise HTTPException(
            503,
            detail={
                "code": "all_reads_failed",
                "message": f"Failed to read any of {read_failures} result file(s).",
            },
        )

    if format == "json":
        body = json.dumps({"job_id": job_id, "hits": all_hits, "total": len(all_hits)}, default=str)
        return StreamingResponse(
            iter([body.encode("utf-8")]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{job_id}_results.json"'},
        )

    # CSV / TSV. Include extra columns only when at least one hit has them so
    # the file does not get a bunch of blank trailing columns for vanilla
    # `-outfmt 6` output.
    delimiter = "\t" if format == "tsv" else ","
    extras_present = [col for col in EXPORT_EXTRA_COLUMNS if any(col in hit for hit in all_hits)]
    columns = list(EXPORT_DEFAULT_COLUMNS) + extras_present
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, delimiter=delimiter, extrasaction="ignore")
    writer.writeheader()
    for hit in all_hits:
        writer.writerow(hit)
    ext = "tsv" if format == "tsv" else "csv"
    mime = "text/tab-separated-values" if format == "tsv" else "text/csv"
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8")]),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{job_id}_results.{ext}"'},
    )


@blast_router.get("/jobs/{job_id}/results/{file_id}")
def blast_job_result_file(
    job_id: str = Path(...),
    file_id: str = Path(..., min_length=1, max_length=512, pattern=r"^[A-Za-z0-9._-]+$"),
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> StreamingResponse:
    """Stream one result file by file_id through the api sidecar.

    Local result file ids are deterministic URL-safe encodings of blob names.
    External OpenAPI jobs keep their sibling-generated ids such as
    `result-001`. The browser never receives a SAS URL in either path.
    """
    _ensure_job_read_allowed(job_id, caller)
    try:
        from api.services.storage_data import (
            decode_blob_file_id,
            result_media_type,
            safe_download_filename,
            stream_blob_bytes,
        )

        blob_path = decode_blob_file_id(file_id)
        if blob_path is not None:
            if blob_path != job_id and not blob_path.startswith(f"{job_id}/"):
                raise HTTPException(
                    400,
                    detail={
                        "code": "invalid_file_id",
                        "message": "file_id does not belong to this job",
                    },
                )
            if not storage_account:
                raise HTTPException(
                    400,
                    detail={
                        "code": "missing_storage_account",
                        "message": "storage_account is required for local result file downloads.",
                    },
                )
            from api.services import get_credential

            filename = safe_download_filename(blob_path)
            return StreamingResponse(
                stream_blob_bytes(get_credential(), storage_account, "results", blob_path),
                media_type=result_media_type(filename),
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_file_id", "message": str(exc)}) from exc
    except Exception as exc:
        LOGGER.warning("local result stream failed: %s", type(exc).__name__)

    try:
        from api.services import external_blast

        downloaded = external_blast.stream_file(job_id, file_id)
        return StreamingResponse(
            downloaded.chunks,
            media_type=downloaded.media_type,
            headers={"Content-Disposition": f'attachment; filename="{downloaded.filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning(
            "external result stream failed job_id=%s file_id=%s: %s",
            job_id,
            file_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            detail={
                "code": "result_stream_unavailable",
                "message": (
                    "Result file could not be streamed from local storage or external OpenAPI."
                ),
            },
        ) from exc


# ===========================================================================
# /api/warmup/* — Celery task placeholder
# ===========================================================================
warmup_router = APIRouter(prefix="/api/warmup", tags=["warmup"])


def _resolve_warmup_db_name(body: dict[str, Any]) -> str:
    """Pick the database name out of either the new SPA shape (`db` /
    `db_display_name`) or the legacy `database_name` shape. Returns the
    bare DB name (e.g. ``16S_ribosomal_RNA``) — strips any
    ``blast-db/`` container prefix the SPA sends with `db`.
    """
    raw = body.get("database_name") or body.get("db_display_name") or body.get("db") or ""
    if isinstance(raw, str) and "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return str(raw or "").strip()


@warmup_router.put("/auto-preference")
def warmup_auto_preference_put(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.services.auto_warmup import normalise_preference, save_auto_warmup_preference

    try:
        pref = normalise_preference(
            {**body, "owner_oid": caller.object_id, "tenant_id": caller.tenant_id}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved = save_auto_warmup_preference(pref)
    return {"status": "saved", "preference": saved.to_dict()}


@warmup_router.get("/auto-preference")
def warmup_auto_preference_get(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    from api.services.auto_warmup import get_auto_warmup_preference

    pref = get_auto_warmup_preference(subscription_id, resource_group, cluster_name)
    if pref is None:
        return {"preference": None}
    return {"preference": pref.to_dict()}


@warmup_router.post("/start")
def warmup_start(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    database_name = _resolve_warmup_db_name(body)
    # Create job state
    try:
        from datetime import datetime

        from api.services.state_repo import JobState, JobStateRepository

        now = datetime.now(UTC).isoformat(timespec="seconds")
        repo = JobStateRepository()
        state = JobState(
            job_id=job_id,
            type="warmup",
            status="queued",
            phase="queued",
            owner_oid=caller.object_id,
            tenant_id=caller.tenant_id,
            created_at=now,
            updated_at=now,
            payload=body,
        )
        repo.create(state)
    except Exception as exc:
        LOGGER.warning("failed to create warmup job state: %s", exc)

    try:
        num_nodes = int(body.get("num_nodes") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "num_nodes must be an integer") from exc

    result = _safe_send_task(
        "api.tasks.storage.warmup_database",
        queue="storage",
        job_id=job_id,
        subscription_id=body.get("subscription_id", ""),
        resource_group=body.get("resource_group", ""),
        storage_account=body.get("storage_account", ""),
        database_name=database_name,
        cluster_name=body.get("aks_cluster_name") or body.get("cluster_name", ""),
        machine_type=body.get("machine_type", ""),
        num_nodes=num_nodes,
        acr_resource_group=body.get("acr_resource_group", ""),
        acr_name=body.get("acr_name", ""),
        program=body.get("program", "blastn"),
        caller_oid=caller.object_id,
    )
    try:
        from api.services.state_repo import JobStateRepository

        JobStateRepository().update(job_id, task_id=result.id)
    except Exception as exc:
        LOGGER.warning("failed to attach warmup task id: %s", exc)
    # The SPA's WarmupSection polls `/warmup/{instance_id}/status`, where
    # `instance_id` is the Celery task id. We expose all three aliases so
    # both the new SPA and any legacy callers keep working.
    return {
        "id": job_id,
        "instance_id": result.id,
        "task_id": result.id,
        "db": database_name,
        "statusQueryGetUri": f"/api/tasks/{result.id}",
        "status": "queued",
    }


@warmup_router.post("/release")
def warmup_release(
    body: dict[str, Any] = _WARMUP_RELEASE_BODY,
    caller: CallerIdentity = _WARMUP_RELEASE_CALLER,
) -> dict[str, Any]:
    database_name = _resolve_warmup_db_name(body)
    subscription_id = str(body.get("subscription_id") or "")
    resource_group = str(body.get("resource_group") or "")
    cluster_name = str(body.get("aks_cluster_name") or body.get("cluster_name") or "")
    if not database_name:
        raise HTTPException(400, "database_name is required")
    if not resource_group:
        raise HTTPException(400, "resource_group is required")
    if not cluster_name:
        raise HTTPException(400, "aks_cluster_name is required")

    from api.services import get_credential
    from api.services.k8s_monitoring import k8s_release_warmup_cache

    try:
        result = k8s_release_warmup_cache(
            get_credential(),
            subscription_id,
            resource_group,
            cluster_name,
            database_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        LOGGER.warning("warmup release failed: %s", type(exc).__name__)
        raise HTTPException(
            503,
            detail={
                "code": "warmup_release_failed",
                "message": f"Could not release warm cache: {type(exc).__name__}",
            },
        ) from exc

    return {"db": database_name, **result}


@warmup_router.get("/{instance_id}/status")
def warmup_status(
    instance_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return warmup task status mapped to the SPA's orchestrator-style shape.

    The SPA's `WarmupSection` was originally written against a Durable
    Functions orchestrator (``runtime_status`` ∈ {Pending, Running,
    Completed, Failed, Terminated}) and a ``custom_status``/``output``
    payload. We translate the Celery ``AsyncResult`` to that shape so
    the SPA can be migrated incrementally.
    """
    from celery.result import AsyncResult

    from api.celery_app import celery_app

    result = AsyncResult(instance_id, app=celery_app)
    status = (result.status or "PENDING").upper()
    runtime_status = {
        "PENDING": "Pending",
        "RECEIVED": "Pending",
        "STARTED": "Running",
        "RETRY": "Running",
        "PROGRESS": "Running",
        "SUCCESS": "Completed",
        "FAILURE": "Failed",
        "REVOKED": "Terminated",
    }.get(status, "Running")

    custom_status: dict[str, Any] = {"phase": status.lower()}
    output: dict[str, Any] | None = None

    if not result.ready():
        info = result.info if isinstance(result.info, dict) else None
        if info:
            custom_status.update({k: v for k, v in info.items() if k != "exc_type"})
    elif result.successful():
        payload = result.result if isinstance(result.result, dict) else {}
        db_name = str(payload.get("database") or payload.get("db") or "")
        payload_status = str(payload.get("status", "")).lower()
        succeeded = payload_status in {"completed", "succeeded", "success"}
        custom_status.update({"phase": "completed", "db": db_name})
        output = {
            "status": "succeeded" if succeeded else "failed",
            "db": db_name,
        }
        if not succeeded and payload.get("error"):
            output["error"] = str(payload.get("error"))[:500]
    else:
        # FAILURE / REVOKED
        err = ""
        try:
            err = str(result.result or result.info or "")[:500]
        except Exception:
            err = "task failed"
        custom_status.update({"phase": "failed"})
        output = {"status": "failed", "db": "", "error": err}

    return {
        "instance_id": instance_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }


# ===========================================================================
# /api/audit/log — best-effort read from jobhistory; empty if unavailable
# ===========================================================================
audit_router = APIRouter(prefix="/api/audit", tags=["audit"])


@audit_router.get("/log")
def audit_log(
    limit: int = Query(default=200, ge=1, le=1000),
    action: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return recent audit events from the jobhistory table."""
    try:
        from api.services.state_repo import JobStateRepository

        repo = JobStateRepository()
        # List recent jobs for the caller, then collect their history
        jobs = repo.list_for_owner(caller.object_id, limit=50)
        events: list[dict[str, Any]] = []
        for job in jobs[:20]:  # cap to avoid excessive table queries
            history = repo.get_history(job.job_id, limit=20)
            for h in history:
                if action and h.get("event") != action:
                    continue
                events.append(
                    {
                        "job_id": job.job_id,
                        "job_type": job.type,
                        "event": h.get("event", ""),
                        "ts": h.get("ts", ""),
                        "payload": h.get("payload_json", ""),
                    }
                )
                if len(events) >= limit:
                    break
            if len(events) >= limit:
                break
        events.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return {"events": events[:limit]}
    except Exception as exc:
        LOGGER.warning("audit_log failed: %s", exc)
        return {"events": [], "error": str(exc)[:200]}
