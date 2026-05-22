"""/api/blast database catalogue, sharding, and oracle routes.

Responsibility: /api/blast database catalogue, sharding, and oracle routes
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `blast_databases`, `blast_database_shard`, `blast_database_order_oracle`,
`blast_databases_check_updates`, `blast_databases_versions`, `blast_databases_build_stub`,
`blast_database_preview`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py api/tests/test_blast_databases_preview.py
api/tests/test_blast_databases_check_updates.py`.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _SHARD_LOCK_REGISTRY,
    _SHARD_LOCK_REGISTRY_GUARD,
    _SHARD_STALE_SECONDS,
    _maybe_open_local_storage_access,
    _stub_log,
)
from api.routes.blast.common import LAB_TOOL_PENDING

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/databases")
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

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_databases",
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


@router.post("/databases/{db_name}/shard")
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
    from api.services.storage_data import _blob_service

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
    _maybe_open_local_storage_access(
        cred,
        sub,
        storage_rg,
        account_name,
        context="blast_database_shard",
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
    # ETag-aware metadata write. Concurrent prepare-db / warmup writers can
    # not race the same metadata blob anymore — `_update_metadata` retries on
    # 412 instead of blindly overwriting.
    try:
        from api.routes.storage.prepare_db import _update_metadata as _update_md

        def _pre_mutator(meta: dict[str, Any]) -> dict[str, Any]:
            meta["db_name"] = db_name
            meta["sharding_in_progress"] = True
            meta["sharding_started_at"] = started_at
            meta.pop("sharding_error", None)
            return meta

        _update_md(cc, db_name, account_name, _pre_mutator)
    except Exception as exc:
        lock.release()
        LOGGER.warning(
            "blast_database_shard: pre-state write failed db=%s: %s",
            db_name,
            type(exc).__name__,
        )
        raise HTTPException(502, f"metadata pre-write failed: {type(exc).__name__}") from exc

    # Audit — records the sharding action against the caller so /api/audit/log
    # surfaces it alongside BLAST / warmup operations.
    try:
        from api.services.db_ops_audit import record_db_op

        record_db_op(
            op="shard",
            caller=caller,
            account_name=account_name,
            db_name=db_name,
        )
    except Exception as exc:
        LOGGER.debug("shard audit record skipped: %s", type(exc).__name__)

    LOGGER.info(
        "blast_database_shard accepted oid=%s db=%s account=%s",
        caller.object_id,
        db_name,
        account_name,
    )

    def _do_shard() -> None:
        """Background worker — owns the lock for the lifetime of the call."""
        from api.routes.storage.prepare_db import _update_metadata as _update_md
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
                cc2 = svc2.get_container_client(DEFAULT_CONTAINER)

                def _err_mut(meta: dict[str, Any]) -> dict[str, Any]:
                    meta["sharding_in_progress"] = False
                    meta["sharding_error"] = err_msg
                    return meta

                _update_md(cc2, db_name, account_name, _err_mut)
            except Exception as inner:
                LOGGER.warning(
                    "blast_database_shard error-state write failed db=%s: %s",
                    db_name,
                    type(inner).__name__,
                )
            finally:
                lock.release()
            return

        # Success — merge the summary into metadata via ETag-aware writer
        # so a concurrent prepare-db / warmup writer cannot clobber the
        # shard fields.
        try:
            local_cred = _get_cred()
            svc2 = _blob_service(local_cred, account_name)
            cc2 = svc2.get_container_client(DEFAULT_CONTAINER)

            def _ok_mut(meta: dict[str, Any]) -> dict[str, Any]:
                meta["sharding_in_progress"] = False
                meta.pop("sharding_error", None)
                meta["sharded"] = bool(summary.get("shard_sets"))
                meta["shard_sets"] = summary.get("shard_sets", [])
                if meta.get("source_version"):
                    meta["shard_source_version"] = meta.get("source_version")
                meta["sharded_at"] = datetime.now(UTC).isoformat()
                if summary.get("total_bytes"):
                    meta.setdefault("total_bytes", summary["total_bytes"])
                for key in (
                    "total_letters",
                    "total_sequences",
                    "bytes_to_cache",
                    "bytes_total",
                ):
                    if summary.get(key):
                        meta.setdefault(key, summary[key])
                return meta

            _update_md(cc2, db_name, account_name, _ok_mut)
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
    from api.services.db_order_oracle import (
        ORACLE_PARTS_DIR,
        ORACLE_PREFIX_ROOT,
        build_db_order_oracle_job_plan,
        oracle_status_blob_path,
    )
    from api.services.image_tags import IMAGE_TAGS
    from api.services.k8s_monitoring import (
        k8s_ensure_job_manifests,
        k8s_ready_warmup_node_names,
        k8s_warmup_status,
    )
    from api.services.storage_data import list_databases, upload_blob_text

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
    _maybe_open_local_storage_access(
        cred,
        sub,
        storage_rg,
        account_name,
        context="blast_database_order_oracle",
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
        from api.services.db_ops_audit import record_db_op

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
        caller.object_id,
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


@router.get("/databases/check-updates")
def blast_databases_check_updates(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return NCBI's current snapshot plus a per-DB update list.

    Two-tier response:

    * ``latest_version`` — the bucket-wide ``latest-dir`` tag (back-compat).
    * ``updates_available`` — per-DB list ``[{db, snapshot, signature_etag,
      stored_etag, stored_source_version}]`` for every downloaded DB whose
      NCBI signature ETag differs from the value last written into
      ``{db}-metadata.json``. The ETag comparison replaces the previous
      ``source_version != latest-dir`` heuristic, which produced false
      positives every time NCBI rotated ``latest-dir`` even when the
      requested DB itself had not changed.

    Optional query params drive the per-DB enrichment. With no storage
    account the response is the back-compat shape ``{latest_version,
    updates_available: []}``.
    """
    from api.routes.storage.common import (
        NcbiAccessDenied,
        NcbiUnavailable,
    )

    base: dict[str, Any] = {
        "latest_version": "",
        "updates_available": [],
    }

    try:
        from api.routes.storage.common import _resolve_latest_dir
        from api.services.ncbi_catalogue import preview_database

        base["latest_version"] = _resolve_latest_dir()
    except NcbiAccessDenied as exc:
        LOGGER.warning("check-updates: NCBI denied: %s", type(exc).__name__)
        return {
            **base,
            "degraded": True,
            "degraded_reason": "ncbi_denied",
            "message": "NCBI bucket refused the request (likely throttling).",
        }
    except NcbiUnavailable as exc:
        LOGGER.warning("check-updates: NCBI unavailable: %s", type(exc).__name__)
        return {
            **base,
            "degraded": True,
            "degraded_reason": "ncbi_unreachable",
            "message": f"Could not contact NCBI: {type(exc).__name__}",
        }
    except Exception as exc:
        LOGGER.warning("check-updates failed: %s", type(exc).__name__)
        return {
            **base,
            "degraded": True,
            "degraded_reason": "ncbi_unreachable",
            "message": f"Could not contact NCBI: {type(exc).__name__}",
        }

    if not (storage_account and resource_group):
        return base

    # Per-DB enrichment — only runs when the caller passes storage scope.
    from api.services import get_credential
    from api.services.storage_data import list_databases

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_databases_check_updates",
    )
    try:
        downloaded = list_databases(cred, storage_account)
    except Exception as exc:
        LOGGER.warning("check-updates list_databases failed: %s", type(exc).__name__)
        return base

    updates: list[dict[str, Any]] = []
    for db in downloaded:
        if not isinstance(db, dict):
            continue
        name = str(db.get("name") or "").strip()
        if not name:
            continue
        stored_etag = str(db.get("signature_etag") or "").strip()
        stored_composite = str(db.get("composite_signature") or "").strip()
        stored_version = str(db.get("source_version") or "").strip()
        try:
            preview = preview_database(name)
        except (NcbiAccessDenied, NcbiUnavailable, ValueError) as exc:
            LOGGER.debug(
                "check-updates: preview %s skipped: %s",
                name,
                type(exc).__name__,
            )
            continue
        if not preview.get("available"):
            continue
        ncbi_etag = str(preview.get("signature_etag") or "").strip()
        ncbi_composite = str(preview.get("composite_signature") or "").strip()
        ncbi_snapshot = str(preview.get("snapshot") or "").strip()
        # Update detection precedence (most precise first):
        #   1. composite_signature — hashes N md5 ETags, detects updates on
        #      any sampled shard (multi-volume safe).
        #   2. signature_etag — single .tar.gz.md5 ETag (legacy DBs prepared
        #      before composite signatures landed).
        #   3. source_version vs snapshot — coarsest fallback for DBs whose
        #      metadata predates ETag tracking.
        if stored_composite:
            changed = bool(ncbi_composite) and ncbi_composite != stored_composite
        elif stored_etag:
            changed = bool(ncbi_etag) and ncbi_etag != stored_etag
        else:
            changed = bool(ncbi_snapshot) and ncbi_snapshot != stored_version
        if changed:
            updates.append(
                {
                    "db": name,
                    "snapshot": ncbi_snapshot,
                    "signature_etag": ncbi_etag,
                    "composite_signature": ncbi_composite or None,
                    "stored_etag": stored_etag or None,
                    "stored_composite_signature": stored_composite or None,
                    "stored_source_version": stored_version or None,
                }
            )

    base["updates_available"] = updates
    return base


@router.get("/databases/{db_name}/preview")
def blast_database_preview(
    db_name: str,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return a dry-run NCBI snapshot summary for a DB the user might pull.

    Used by the SPA modal to show snapshot id, file count, estimated bytes,
    and last-modified BEFORE the user clicks Download. ``available=False``
    means the DB is missing from the current S3 snapshot (likely FTP-only or
    mid-publish) — the SPA surfaces that as a clear hint instead of letting
    the Download button silently fail with a 404 mid-copy.
    """
    from api.routes.storage.common import (
        NcbiAccessDenied,
        NcbiUnavailable,
    )
    from api.services.ncbi_catalogue import RE_DB_NAME, preview_database

    if not RE_DB_NAME.match(db_name):
        raise HTTPException(400, "invalid db_name")
    try:
        return preview_database(db_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)[:200]) from exc
    except NcbiAccessDenied as exc:
        LOGGER.warning("preview %s: NCBI denied: %s", db_name, type(exc).__name__)
        raise HTTPException(
            502,
            "NCBI bucket refused the request (likely rate-limited); retry shortly.",
        ) from exc
    except NcbiUnavailable as exc:
        LOGGER.warning("preview %s: NCBI unavailable: %s", db_name, type(exc).__name__)
        raise HTTPException(
            502,
            f"Could not contact NCBI: {type(exc).__name__}",
        ) from exc


@router.get("/databases/versions")
def blast_databases_versions(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return a flat per-DB version listing for the "DB Versions" tab.

    This is a focused projection over ``list_databases()`` — same data
    source as ``GET /api/blast/databases`` but reshaped to match the
    ``DbVersionMeta`` contract the SPA expects
    (``web/src/api/blastTools.ts``). The route is read-only and
    intentionally keeps the response small (no shard/warmup/oracle
    fields) so the tab can render quickly.
    """
    if not storage_account or not resource_group:
        return {"versions": [], "total": 0}

    from api.services import get_credential
    from api.services.storage_data import classify_storage_failure, list_databases

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_databases_versions",
    )
    try:
        databases = list_databases(cred, storage_account)
    except Exception as exc:
        LOGGER.warning("blast_databases_versions failed: %s", type(exc).__name__)
        return {
            "versions": [],
            "total": 0,
            **classify_storage_failure(
                cred, subscription_id, resource_group, storage_account, exc
            ),
        }

    versions: list[dict[str, Any]] = []
    for db in databases:
        if not isinstance(db, dict):
            continue
        name = str(db.get("name") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {
            "db_name": name,
            "source": db.get("source"),
            "source_version": db.get("source_version"),
            "created_at": db.get("downloaded_at"),
            "_last_modified": db.get("last_modified"),
        }
        # Optional enrichments — only emit when the underlying .njs /
        # metadata blob actually carried the field, so the SPA badge
        # logic ("—" for missing) stays honest.
        if isinstance(db.get("molecule_type"), str):
            entry["db_type"] = db["molecule_type"]
        if isinstance(db.get("title"), str):
            entry["title"] = db["title"]
        if isinstance(db.get("update_date"), str):
            entry["version_tag"] = db["update_date"]
        versions.append(entry)

    versions.sort(key=lambda v: str(v.get("db_name") or ""))
    return {"versions": versions, "total": len(versions)}


# --- Lab Tools: pre-flight estimators and sidecar-dependent utilities ---
#
# These endpoints are referenced by the SPA (`web/src/api/endpoints.ts`,
# `web/src/pages/tools/ToolTabs.tsx`, `web/src/pages/DatabaseBuilder.tsx`)
# but their Celery tasks have not been ported from the legacy Function App
# yet. Returning a structured 503 here turns silent 404s into a clear
# "backend pending" signal the UI can render.


@router.post("/databases/build")
def blast_databases_build_stub(
    _body: dict[str, Any] = Body(default_factory=dict),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    _stub_log("blast/databases/build")
    raise HTTPException(503, detail=LAB_TOOL_PENDING)


# --- Schedules ---
