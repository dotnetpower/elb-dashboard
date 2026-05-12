"""Resource provisioning routes (``/api/resources/*``).

Wizard-driven idempotent creation of the workspace's foundational
resources: resource group, storage account (HNS), and ACR.
"""

from __future__ import annotations

import json
import logging

import azure.durable_functions as df
import azure.functions as func

from _http_utils import _error_response, _json_response
from auth.token import AuthError, validate_bearer_token
from services import monitoring as monitoring_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()


@bp.route(route="resources/ensure-rg", methods=["POST"])
def ensure_resource_group(req: func.HttpRequest) -> func.HttpResponse:
    """Create a resource group if it doesn't exist. Idempotent."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "region"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    from services import network as net_svc

    cred = credential_for_caller(identity.raw_token)
    try:
        net_svc.ensure_resource_group(
            cred, body["subscription_id"], body["resource_group"], body["region"],
        )
    except Exception as exc:
        LOGGER.warning("ensure_resource_group failed: %s", exc)
        return _error_response(500, f"failed to create resource group: {sanitise(str(exc))}")

    LOGGER.info(
        "ensure_resource_group by oid=%s rg=%s",
        identity.object_id, body["resource_group"],
    )
    return _json_response({
        "resource_group": body["resource_group"],
        "region": body["region"],
        "status": "created",
    })


@bp.route(route="resources/ensure-storage", methods=["POST"])
def ensure_storage_account(req: func.HttpRequest) -> func.HttpResponse:
    """Create a storage account with HNS if it doesn't exist. Idempotent."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "account_name", "region"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    cred = credential_for_caller(identity.raw_token)
    try:
        monitoring_svc.ensure_storage_account(
            cred,
            body["subscription_id"],
            body["resource_group"],
            body["account_name"],
            body["region"],
            caller_oid=identity.object_id,
        )
    except Exception as exc:
        LOGGER.warning("ensure_storage_account failed: %s", exc)
        return _error_response(500, f"failed to create storage account: {sanitise(str(exc))}")

    LOGGER.info(
        "ensure_storage_account by oid=%s account=%s",
        identity.object_id, body["account_name"],
    )
    return _json_response({
        "account_name": body["account_name"],
        "region": body["region"],
        "status": "created",
    })


@bp.route(route="resources/ensure-acr", methods=["POST"])
def ensure_acr(req: func.HttpRequest) -> func.HttpResponse:
    """Create an ACR if it doesn't exist. Idempotent."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = json.loads(req.get_body() or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, f"invalid JSON: {exc}")

    required = {"subscription_id", "resource_group", "registry_name", "region"}
    missing = required - body.keys()
    if missing:
        return _error_response(400, f"missing fields: {sorted(missing)}")

    cred = credential_for_caller(identity.raw_token)
    try:
        monitoring_svc.ensure_acr(
            cred,
            body["subscription_id"],
            body["resource_group"],
            body["registry_name"],
            body["region"],
            caller_oid=identity.object_id,
        )
    except Exception as exc:
        LOGGER.warning("ensure_acr failed: %s", exc)
        return _error_response(500, f"failed to create ACR: {sanitise(str(exc))}")

    LOGGER.info(
        "ensure_acr by oid=%s registry=%s",
        identity.object_id, body["registry_name"],
    )
    return _json_response({
        "registry_name": body["registry_name"],
        "region": body["region"],
        "status": "created",
    })
