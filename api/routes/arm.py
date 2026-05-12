"""ARM discovery + Resource Group tag routes (``/api/arm/*``).

Backend-proxied Azure Resource Manager calls so the SPA can use its
MSAL token without needing direct ARM access from the browser.
"""

from __future__ import annotations

import json
import logging

import azure.durable_functions as df
import azure.functions as func

from _http_utils import (
    _error_response,
    _json_response,
    _require_query,
)
from auth.token import AuthError, validate_bearer_token
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()

ELB_TAG_PREFIX = "elb-"


@bp.route(route="arm/subscriptions", methods=["GET"])
def list_subscriptions(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    from azure.mgmt.resource import SubscriptionClient

    cred = credential_for_caller(identity.raw_token)
    client = SubscriptionClient(cred)
    subs = []
    for s in client.subscriptions.list():
        state = s.state
        subs.append({
            "subscriptionId": s.subscription_id,
            "displayName": s.display_name,
            "state": state.value if hasattr(state, "value") else str(state or "Unknown"),
            "tenantId": s.tenant_id,
        })
    subs.sort(key=lambda x: x["displayName"])
    return _json_response(subs)


@bp.route(route="arm/subscriptions/{subscription_id}/resource-groups", methods=["GET"])
def list_resource_groups_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    if not subscription_id:
        return _error_response(400, "subscription_id missing")

    from services.azure_clients import resource_client

    cred = credential_for_caller(identity.raw_token)
    rc = resource_client(cred, subscription_id)
    groups = [{"name": g.name, "location": g.location, "tags": g.tags or {}}
              for g in rc.resource_groups.list()]
    groups.sort(key=lambda x: x["name"])
    return _json_response(groups)


@bp.route(route="arm/resource-group/tags", methods=["GET"])
def get_rg_tags(req: func.HttpRequest) -> func.HttpResponse:
    """Read ELB-related tags from a resource group."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    params, err = _require_query(req, "subscription_id", "resource_group")
    if err:
        return err
    cred = credential_for_caller(identity.raw_token)
    from services.azure_clients import resource_client
    rc = resource_client(cred, params["subscription_id"])
    try:
        rg = rc.resource_groups.get(params["resource_group"])
        tags = {k: v for k, v in (rg.tags or {}).items() if k.startswith(ELB_TAG_PREFIX)}
        return _json_response({"resource_group": rg.name, "tags": tags})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="arm/resource-group/tags", methods=["POST"])
def set_rg_tags(req: func.HttpRequest) -> func.HttpResponse:
    """Write ELB-related tags to a resource group (merge, not replace)."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    rg_name = body.get("resource_group", "")
    new_tags = body.get("tags", {})
    if not sub or not rg_name or not new_tags:
        return _error_response(400, "subscription_id, resource_group, tags required")
    for k in new_tags:
        if not k.startswith(ELB_TAG_PREFIX):
            return _error_response(400, f"tag key must start with '{ELB_TAG_PREFIX}': {k}")
    cred = credential_for_caller(identity.raw_token)
    from services.azure_clients import resource_client
    rc = resource_client(cred, sub)
    try:
        rg = rc.resource_groups.get(rg_name)
        merged = {**(rg.tags or {}), **new_tags}
        rc.resource_groups.create_or_update(rg_name, {"location": rg.location, "tags": merged})
        return _json_response({"resource_group": rg_name, "tags": {k: v for k, v in merged.items() if k.startswith(ELB_TAG_PREFIX)}})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)))


@bp.route(route="arm/subscriptions/{subscription_id}/resource-groups/{rg}/storage-accounts", methods=["GET"])
def list_storage_accounts_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    rg = req.route_params.get("rg")
    if not subscription_id or not rg:
        return _error_response(400, "subscription_id and rg required")

    from services.azure_clients import storage_client as sc

    cred = credential_for_caller(identity.raw_token)
    client = sc(cred, subscription_id)
    accounts = [{"name": a.name, "location": a.location}
                for a in client.storage_accounts.list_by_resource_group(rg)]
    accounts.sort(key=lambda x: x["name"])
    return _json_response(accounts)


@bp.route(route="arm/subscriptions/{subscription_id}/resource-groups/{rg}/acrs", methods=["GET"])
def list_acrs_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    rg = req.route_params.get("rg")
    if not subscription_id or not rg:
        return _error_response(400, "subscription_id and rg required")

    from services.azure_clients import acr_client

    cred = credential_for_caller(identity.raw_token)
    client = acr_client(cred, subscription_id)
    registries = [{"name": r.name, "location": r.location,
                   "loginServer": r.login_server}
                  for r in client.registries.list_by_resource_group(rg)]
    registries.sort(key=lambda x: x["name"])
    return _json_response(registries)


@bp.route(route="arm/subscriptions/{subscription_id}/resource-groups/{rg}/vms", methods=["GET"])
def list_vms_route(req: func.HttpRequest) -> func.HttpResponse:
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    subscription_id = req.route_params.get("subscription_id")
    rg = req.route_params.get("rg")
    if not subscription_id or not rg:
        return _error_response(400, "subscription_id and rg required")

    from services.azure_clients import compute_client as cc

    cred = credential_for_caller(identity.raw_token)
    client = cc(cred, subscription_id)
    vms = [{"name": v.name, "location": v.location}
           for v in client.virtual_machines.list(rg)]
    vms.sort(key=lambda x: x["name"])
    return _json_response(vms)
