"""/api/blast database order-oracle route.

Synchronous DB-order-oracle build trigger for a warmed, sharded database.
Split out of `api/routes/blast/databases.py` so the catalogue, sharding, and
order-oracle concerns each own a single-responsibility route module under the
shared `blast_router`.

Responsibility: Accept `POST /databases/{db}/oracle`, gate on AKS health +
    storage/warmup readiness, map every warmed shard to a Ready node, write the
    building-status blob, and dispatch the per-shard `blastdbcmd` Jobs.
Edit boundaries: HTTP validation + readiness gating + dispatch only; the Job
    plan math lives in `api/services/db/order_oracle.py` and the K8s apply in
    `api/services/k8s/monitoring.py`.
Key entry points: `blast_database_order_oracle`.
Risky contracts: Every non-health `/api/*` route must enforce `require_caller`.
    The route MUST refuse (409) when storage / warmup source versions disagree
    so an oracle is never built against a stale DB generation.
Validation: `uv run pytest -q api/tests/test_route_contracts.py
    api/tests/test_blast_results_routes.py`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _maybe_open_local_storage_access
from api.routes.blast.databases import (
    _DB_NAME_RE,
    _RESOURCE_GROUP_RE,
    _STORAGE_ACCOUNT_RE,
    _SUBSCRIPTION_RE,
)
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.post("/databases/{db_name}/oracle")
def blast_database_order_oracle(
    db_name: str,
    body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Create cached DB-order oracle parts for a warmed sharded database.

    This is intentionally a user/DB-update action, not part of BLAST submit.
    The created Jobs run on the already-warmed nodes, dump each shard's BLAST DB
    accession order with ``blastdbcmd``, and upload the part files to Storage.
    Later BLAST submissions attach only a tiny pointer list, so the search path
    remains fast.
    """

    import json
    from datetime import datetime

    from api.services import get_credential
    from api.services.db.order_oracle import (
        ORACLE_PARTS_DIR,
        ORACLE_PREFIX_ROOT,
        build_db_order_oracle_job_plan,
        oracle_status_blob_path,
    )
    from api.services.image_tags import IMAGE_TAGS
    from api.services.k8s.monitoring import (
        k8s_ensure_job_manifests,
        k8s_ready_warmup_node_names,
        k8s_warmup_status,
    )
    from api.services.storage.data import list_databases, upload_blob_text

    sub = str(body.get("subscription_id") or "")
    storage_rg = str(body.get("resource_group") or "")
    account_name = str(body.get("account_name") or body.get("storage_account") or "")
    cluster_name = str(body.get("cluster_name") or body.get("aks_cluster_name") or "")
    acr_name = str(body.get("acr_name") or "")
    image = str(body.get("image") or "")
    if not image and acr_name:
        image = f"{acr_name.strip().lower()}.azurecr.io/ncbi/elb:{IMAGE_TAGS['ncbi/elb']}"
    if not all([sub, storage_rg, account_name, cluster_name, image]):
        raise HTTPException(
            400,
            (
                "subscription_id, resource_group, account_name, cluster_name, "
                "and acr_name or image required"
            ),
        )

    if not _DB_NAME_RE.match(db_name):
        raise HTTPException(400, "invalid db_name")
    if not _SUBSCRIPTION_RE.match(sub):
        raise HTTPException(400, "invalid subscription_id")
    if not _RESOURCE_GROUP_RE.match(storage_rg):
        raise HTTPException(400, "invalid resource_group")
    if not _STORAGE_ACCOUNT_RE.match(account_name):
        raise HTTPException(400, "invalid account_name")

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        sub,
        storage_rg,
        account_name,
        context="blast_database_order_oracle",
    )

    # ARM-level powerState gate first — a stopped cluster yields a clean 409
    # instead of letting the K8s warmup/job calls below time out (~10 s).
    # Mirrors the precheck in /api/storage/prepare-db (mode=aks).
    from api.services.cluster_health import get_cluster_health

    try:
        health = get_cluster_health(cred, sub, storage_rg, cluster_name)
    except Exception as exc:
        LOGGER.debug(
            "cluster_health probe raised for order-oracle build: %s",
            type(exc).__name__,
        )
        health = None
    if health is not None and not health.get("healthy", True):
        reason = health.get("reason")
        power_state = health.get("power_state")
        raise HTTPException(
            status_code=409,
            detail={
                "code": "aks_unavailable",
                "message": (
                    "AKS cluster is not Running "
                    f"(reason={reason}, power_state={power_state}). "
                    "Start the cluster from the dashboard before building the "
                    "order oracle."
                ),
                "cluster_reason": reason,
                "cluster_power_state": power_state,
            },
        )

    db_meta = next(
        (
            item
            for item in list_databases(cred, account_name, "blast-db")
            if isinstance(item, dict) and item.get("name") == db_name
        ),
        None,
    )
    if not isinstance(db_meta, dict):
        raise HTTPException(404, f"database {db_name} is not downloaded")
    storage_source_version = str(db_meta.get("source_version") or "")
    requested_source_version = str(body.get("source_version") or "")
    if requested_source_version and storage_source_version != requested_source_version:
        raise HTTPException(
            409,
            (
                f"database {db_name} source_version changed; refresh before building "
                "the order oracle"
            ),
        )
    if db_meta.get("update_in_progress"):
        raise HTTPException(409, f"database {db_name} is updating; wait for promotion")
    # Reject when the DB's last prepare-db ended in partial/init_failed —
    # the on-disk files may be missing volumes and the oracle would produce
    # an incomplete pointer list. Require a clean Ready state.
    copy_status = db_meta.get("copy_status")
    if isinstance(copy_status, dict):
        phase = str(copy_status.get("phase") or "")
        if phase in {"partial", "init_failed", "copying"}:
            raise HTTPException(
                409,
                (
                    f"database {db_name} download is not Ready (phase={phase}); "
                    "retry Download before building the order oracle"
                ),
            )
    if db_meta.get("shards_stale"):
        raise HTTPException(409, f"database {db_name} shard layouts are stale; rebuild shards")

    warmup = k8s_warmup_status(cred, sub, storage_rg, cluster_name)
    db_status = next(
        (
            item
            for item in warmup.get("databases", [])
            if isinstance(item, dict) and item.get("name") == db_name
        ),
        None,
    )
    if not isinstance(db_status, dict) or db_status.get("status") != "Ready":
        raise HTTPException(
            409,
            f"node-local warmup for {db_name} must be Ready before building its order oracle",
        )
    warm_source_version = str(db_status.get("source_version") or "")
    warm_source_versions = [
        str(item) for item in db_status.get("source_versions", []) or [] if str(item)
    ]
    if db_status.get("status") == "Stale" or len(set(warm_source_versions)) > 1:
        raise HTTPException(409, f"node-local warmup for {db_name} has stale source versions")
    if (
        storage_source_version
        and warm_source_version
        and warm_source_version != storage_source_version
    ):
        raise HTTPException(409, f"node-local warmup for {db_name} is for a stale DB generation")

    pod_nodes: dict[str, str] = {}
    for pod in db_status.get("pod_statuses", []) or []:
        if not isinstance(pod, dict):
            continue
        shard = str(pod.get("shard") or "")
        node = str(pod.get("node") or "")
        if shard and node:
            pod_nodes[shard] = node
    shards = sorted(str(shard) for shard in db_status.get("shards", []) or [] if str(shard))
    if not shards:
        shard_count = int(body.get("shard_count") or db_status.get("total_jobs") or 1)
        shards = [f"{idx:02d}" for idx in range(shard_count)]
    nodes = k8s_ready_warmup_node_names(cred, sub, storage_rg, cluster_name)
    raw_host_paths = db_status.get("shard_host_paths") or {}
    shard_host_paths = raw_host_paths if isinstance(raw_host_paths, dict) else {}
    shard_nodes: list[tuple[str, str] | tuple[str, str, str]] = []
    for idx, shard in enumerate(shards):
        node = pod_nodes.get(shard) or (nodes[idx] if idx < len(nodes) else "")
        if node:
            host_path = shard_host_paths.get(shard)
            if isinstance(host_path, str) and host_path:
                shard_nodes.append((shard, node, host_path))
            else:
                shard_nodes.append((shard, node))
    if len(shard_nodes) != len(shards):
        raise HTTPException(409, "could not map every warmed shard to a Ready node")

    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    source_version = storage_source_version or warm_source_version
    part_prefix = f"{ORACLE_PREFIX_ROOT}/{db_name}/{ORACLE_PARTS_DIR}/{run_id}/"
    status_blob = oracle_status_blob_path(db_name)
    status_payload = {
        "status": "building",
        "run_id": run_id,
        "db_name": db_name,
        "source_version": source_version,
        "started_at": datetime.now(UTC).isoformat(),
        "expected_parts": len(shard_nodes),
        "ready_parts": 0,
        "part_prefix": part_prefix,
        "requested_by": caller.object_id,
    }
    upload_blob_text(
        cred,
        account_name,
        "blast-db",
        status_blob,
        json.dumps(status_payload, sort_keys=True) + "\n",
        content_type="application/json; charset=utf-8",
    )

    plan = build_db_order_oracle_job_plan(
        db_name=db_name,
        storage_account=account_name,
        run_id=run_id,
        shard_nodes=shard_nodes,
        image=image,
    )
    apply_summary = k8s_ensure_job_manifests(
        cred,
        sub,
        storage_rg,
        cluster_name,
        list(plan.jobs),
    )
    if apply_summary.get("error_count"):
        status_payload["status"] = "failed"
        status_payload["error"] = str(apply_summary.get("errors") or [])[:300]
        upload_blob_text(
            cred,
            account_name,
            "blast-db",
            status_blob,
            json.dumps(status_payload, sort_keys=True) + "\n",
            content_type="application/json; charset=utf-8",
        )
        raise HTTPException(502, "oracle Job creation failed")

    # Audit — capture run_id + expected_parts so a later /api/audit/log query
    # can correlate this oracle run with its part blobs.
    try:
        from api.services.db.ops_audit import record_db_op

        record_db_op(
            op="oracle",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
            extra={
                "run_id": run_id,
                "expected_parts": len(shard_nodes),
                "cluster_name": cluster_name,
            },
        )
    except Exception as exc:
        LOGGER.debug("oracle audit record skipped: %s", type(exc).__name__)

    LOGGER.info(
        "db-order oracle accepted oid=%s db=%s run_id=%s parts=%d",
        redact_oid(caller.object_id),
        db_name,
        run_id,
        len(shard_nodes),
    )
    return {
        "accepted": True,
        "db_name": db_name,
        "run_id": run_id,
        "expected_parts": len(shard_nodes),
        "created": apply_summary.get("created", []),
        "existing": apply_summary.get("existing", []),
        "status_blob": status_blob,
        "part_urls": list(plan.part_urls),
    }
