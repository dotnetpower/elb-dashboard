"""Cluster-independent BLAST database catalogue control-plane routes.

Responsibility: Promote the ``elb-openapi`` ``GET /v1/databases`` and
``GET /v1/databases/{db_name}`` reads to the always-on dashboard ``api``
sidecar as ``GET /api/aks/openapi/databases`` and
``GET /api/aks/openapi/databases/{db_name}`` so a caller can enumerate the
prepared BLAST databases and read one database's metadata even while the AKS
cluster (and therefore the in-cluster ``elb-openapi`` service) is stopped.
Edit boundaries: HTTP validation, auth, Storage-scope resolution, and response
status shaping only. The catalogue projection lives in
``api.services.openapi.databases``; the Storage enumeration lives in
``storage.database_catalog_cache``. Do not call ``azure.mgmt.*`` or re-implement
blob listing here.
Key entry points: ``aks_openapi_databases``, ``aks_openapi_database``.
Risky contracts: Both routes are READ-ONLY, so they authenticate via
``require_caller_or_openapi_token`` — the standard MSAL bearer, PLUS (only when
the opt-in ``ALLOW_OPENAPI_TOKEN_AUTH`` gate is on) the shared ``elb-openapi``
``X-ELB-API-Token``. The shared token has no Azure RBAC gate, so it must never be
extended to a cost-bearing / mutating route (ensure-running stays MSAL-only). A
missing Storage account yields HTTP 400 (never 500); a transient Storage outage
yields a degraded payload (503 / network_blocked) classified by
``classify_storage_failure``; a genuinely absent database yields HTTP 404.
Validation: ``uv run pytest -q api/tests/test_aks_openapi_databases.py``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Response

from api.auth import CallerIdentity, require_caller_or_openapi_token
from api.routes._blast_shared import _maybe_open_local_storage_access

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_DEFAULT_CONTAINER = "blast-db"
# Storage account names are 3-24 lowercase alphanumerics.
_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")


def _account_from_endpoint(endpoint: str) -> str:
    """Derive the Storage account name from a blob/table endpoint URL.

    Fallback for deployments whose api revision predates the explicit
    ``STORAGE_ACCOUNT_NAME`` env var but still carries ``AZURE_BLOB_ENDPOINT`` /
    ``AZURE_TABLE_ENDPOINT`` (e.g. ``https://acct.blob.core.windows.net/`` ->
    ``acct``). Returns "" when the endpoint is empty or the leading host label
    is not a valid Storage account name.
    """
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return ""
    try:
        host = urlsplit(endpoint).hostname or ""
    except ValueError:
        return ""
    label = host.split(".", 1)[0].strip().lower()
    return label if _ACCOUNT_RE.match(label) else ""


def _resolve_storage_scope(
    subscription_id: str,
    storage_account: str,
    resource_group: str,
) -> tuple[str, str, str]:
    """Resolve the Storage scope from query params with env fallback.

    The Container App always sets ``STORAGE_ACCOUNT_NAME`` /
    ``AZURE_RESOURCE_GROUP`` / ``AZURE_SUBSCRIPTION_ID`` to the single workload
    account, so the Core "Try it" experience stays one-click even when the
    caller omits the params. As a resilience fallback for older revisions that
    predate the ``STORAGE_ACCOUNT_NAME`` env var, the account name is also
    derived from ``AZURE_BLOB_ENDPOINT`` / ``AZURE_TABLE_ENDPOINT`` (always
    present), so the route never 400s on a deployment that can clearly reach
    its own Storage.
    """
    sub = (subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")).strip()
    account = (storage_account or os.environ.get("STORAGE_ACCOUNT_NAME", "")).strip()
    if not account:
        account = _account_from_endpoint(
            os.environ.get("AZURE_BLOB_ENDPOINT", "")
        ) or _account_from_endpoint(os.environ.get("AZURE_TABLE_ENDPOINT", ""))
    rg = (resource_group or os.environ.get("AZURE_RESOURCE_GROUP", "")).strip()
    return sub, account, rg


@router.get("/openapi/databases")
def aks_openapi_databases(
    response: Response,
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller_or_openapi_token),
) -> dict[str, Any]:
    """List prepared BLAST databases from Storage, independent of cluster state.

    Mirrors the ``elb-openapi`` ``GET /v1/databases`` list shape
    (``{databases: [{name}], count, container}``) but is served by the always-on
    api sidecar, so it answers while the AKS cluster is stopped. ``storage_account``
    (or the ``STORAGE_ACCOUNT_NAME`` env) is required.
    """
    sub, account, rg = _resolve_storage_scope(
        subscription_id, storage_account, resource_group
    )
    if not account:
        response.status_code = 400
        return {
            "status": "error",
            "code": "missing_parameters",
            "message": "storage_account (or STORAGE_ACCOUNT_NAME env) is required.",
            "databases": [],
            "count": 0,
            "container": _DEFAULT_CONTAINER,
        }

    from api.services import get_credential
    from api.services.openapi import databases as db_svc
    from api.services.storage.data import classify_storage_failure

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred, sub, rg, account, context="aks_openapi_databases"
    )
    try:
        return db_svc.list_databases(cred, account)
    except Exception as exc:
        LOGGER.warning("aks_openapi_databases failed: %s", type(exc).__name__)
        response.status_code = 503
        return {
            "databases": [],
            "count": 0,
            "container": _DEFAULT_CONTAINER,
            **classify_storage_failure(cred, sub, rg, account, exc),
        }


@router.get("/openapi/databases/{db_name}")
def aks_openapi_database(
    db_name: str,
    response: Response,
    subscription_id: str = Query(default=""),
    storage_account: str = Query(default=""),
    resource_group: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller_or_openapi_token),
) -> dict[str, Any]:
    """Return one database's metadata from Storage, independent of cluster state.

    Mirrors the ``elb-openapi`` ``GET /v1/databases/{db_name}`` DatabaseMetadata
    shape but is served by the always-on api sidecar. The metadata is read from
    the SAME NCBI metadata blobs as ``elb-openapi``
    (``{db}/{db}-nucl-metadata.json`` / ``-prot-metadata.json``), so single-volume
    databases (16S/18S/ITS) carry a correct ``molecule_type`` / counts / title
    rather than the catalogue cache's null enrichment. Unknown name -> 404;
    transient Storage outage -> degraded 503; missing Storage account -> 400.
    """
    if not _DB_NAME_RE.match(db_name):
        response.status_code = 400
        return {
            "status": "error",
            "code": "invalid_db_name",
            "message": "db_name must match ^[A-Za-z0-9_.-]{1,64}$.",
        }

    sub, account, rg = _resolve_storage_scope(
        subscription_id, storage_account, resource_group
    )
    if not account:
        response.status_code = 400
        return {
            "status": "error",
            "code": "missing_parameters",
            "message": "storage_account (or STORAGE_ACCOUNT_NAME env) is required.",
        }

    from api.services import get_credential
    from api.services.openapi import databases as db_svc
    from api.services.storage.data import classify_storage_failure

    cred = get_credential()
    _maybe_open_local_storage_access(
        cred, sub, rg, account, context="aks_openapi_database"
    )
    try:
        meta = db_svc.get_database(cred, account, db_name)
    except Exception as exc:
        LOGGER.warning("aks_openapi_database failed: %s", type(exc).__name__)
        classified = classify_storage_failure(cred, sub, rg, account, exc)
        response.status_code = (
            404 if classified.get("degraded_reason") == "not_found" else 503
        )
        return classified

    if meta is None:
        response.status_code = 404
        return {
            "status": "error",
            "code": "not_found",
            "message": f"Database {db_name!r} not found.",
        }
    return meta
