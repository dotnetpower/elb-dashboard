"""/api/blast database catalogue routes.

Responsibility: /api/blast database catalogue, recommendation, version, preview, and
check-updates routes (the sharding and order-oracle routes live in sibling modules
`databases_shard.py` / `databases_oracle.py`).
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `blast_databases`, `blast_databases_recommend`, `blast_databases_check_updates`,
`blast_databases_versions`, `blast_databases_build_stub`, `blast_database_preview`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate. The compiled `_*_RE` patterns are the shared validation source-of-truth imported by
the sibling `databases_shard` / `databases_oracle` modules.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py api/tests/test_blast_databases_preview.py
api/tests/test_blast_databases_check_updates.py`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import (
    _maybe_open_local_storage_access,
    _stub_log,
)
from api.routes.blast.common import LAB_TOOL_PENDING
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()

# Module-level compiled validation patterns (called from blast_database_check_updates
# and blast_database_preview). Previously these were re-compiled on every request.
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_SUBSCRIPTION_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_RESOURCE_GROUP_RE = re.compile(r"^[A-Za-z0-9._\-()]{1,90}$")
_STORAGE_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")


@router.get("/databases")
def blast_databases(
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    num_nodes: int = Query(default=0, ge=0, le=1000),
    machine_type: str = Query(default=""),
    fresh: bool = Query(default=False),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    if not storage_account or not resource_group:
        return {"databases": []}
    from api.services import get_credential
    from api.services.storage.data import classify_storage_failure
    from api.services.storage.database_catalog_cache import list_databases_cached

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_databases",
    )
    try:
        databases = list_databases_cached(cred, storage_account, force_refresh=fresh)
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
        from api.services.warmup.planner import compute_warmup_feasibility

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


@router.get("/databases/recommend")
def blast_databases_recommend(
    molecule: str = Query(default=""),
    program: str = Query(default=""),
    goal: str = Query(default="identify"),
    taxon: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Recommend one NCBI database plus an alternative for a described search.

    Pure decision logic over a versioned rule table (no Azure data-plane calls).
    Inputs describe the query (``molecule`` or ``program``), the search ``goal``,
    and an optional taxonomic ``taxon`` hint.
    """
    from api.services.blast.db_recommendation import recommend_database

    recommendation = recommend_database(
        molecule=molecule or None,
        program=program or None,
        goal=goal or None,
        taxon=taxon or None,
    )
    return recommendation.as_dict()


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
    from api.services.storage.database_catalog_cache import list_databases_cached

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred,
        subscription_id,
        resource_group,
        storage_account,
        context="blast_databases_check_updates",
    )
    try:
        # Reuse the shared catalogue cache (same as GET /api/blast/databases)
        # instead of re-enumerating the blast-db container. The enumeration is
        # the heavy N+1 path (full blob list + per-DB .njs/metadata reads); the
        # NCBI signature comparison below is what actually drives this route, so
        # the downloaded-DB list is fine to serve from the 300s TTL cache.
        downloaded = list_databases_cached(cred, storage_account)
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
        # Audit P1 #7: sanitise + cap exception text.
        raise HTTPException(400, sanitise(str(exc))[:200]) from exc
    except NcbiAccessDenied as exc:
        LOGGER.warning("preview %s: NCBI denied: %s: %s", db_name, type(exc).__name__, exc)
        raise HTTPException(
            502,
            "NCBI bucket refused the request (likely rate-limited); retry shortly.",
        ) from exc
    except NcbiUnavailable as exc:
        LOGGER.warning(
            "preview %s: NCBI unavailable: %s: %s", db_name, type(exc).__name__, exc
        )
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
    from api.services.storage.data import classify_storage_failure, list_databases

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
