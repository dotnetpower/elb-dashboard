"""/api/aks OpenAPI public-HTTPS routes (enable / disable / status / validator rules).

Responsibility: HTTP surface for the optional public HTTPS endpoint in front of the in-cluster
`elb-openapi` service — enqueue enable/disable, poll task status, expose the cached endpoint
state, and publish the operator-email validation rules the SPA mirrors.
Edit boundaries: Keep HTTP validation, dispatch, and response shaping here; the 9-step pipeline
lives in `api.tasks.openapi.public_https`. The deploy / token / spec / lb-subnet-rbac routes
stay in the sibling `openapi.py`; this router is included onto `openapi.router`.
Key entry points: `aks_openapi_public_https_status`, `aks_openapi_operator_email_rules`,
`aks_openapi_public_https_enable`, `aks_openapi_public_https_disable`,
`aks_openapi_public_https_task_status`, `_validate_operator_email`.
Risky contracts: Every route enforces `require_caller`. `_validate_operator_email` is the
defence-in-depth gate before enqueuing the Celery task (a private-use TLD fails Let's Encrypt
ACME registration); it is re-exported from `openapi.py` so existing imports/tests keep working.
`_safe_delay` is reached via `api.routes._blast_shared` (same as the deploy routes).
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _safe_delay
from api.services.sanitise import redact_oid

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class OpenApiPublicHttpsRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str
    cluster_name: str
    operator_email: str = ""


# IANA reserved + commonly-private TLDs that Let's Encrypt rejects at
# ACME account registration time with
# `urn:ietf:params:acme:error:invalidContact` ("Domain name does not end
# with a valid public suffix (TLD)"). Mirrored in
# `web/src/components/SettingsPanel.tsx::PRIVATE_USE_TLDS` so the SPA
# disables the Enable button before the request even leaves the browser.
_PRIVATE_USE_TLDS: frozenset[str] = frozenset(
    {
        "local",
        "localhost",
        "internal",
        "test",
        "example",
        "invalid",
        "lan",
        "home",
        "corp",
        "private",
    }
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$")


def _validate_operator_email(value: str) -> str:
    """Reject empty / private-TLD emails before enqueuing the Celery task.

    Defence-in-depth for the SPA gate — a stale browser tab or a
    hand-crafted POST must not be able to enqueue
    `setup_openapi_public_https` with `noreply@elb-dashboard.local` and
    silently fail half-way through the install (regression on
    elb-cluster-01, 2026-05-27).
    """
    text = (value or "").strip()
    if not text or len(text) > 254 or not _EMAIL_RE.match(text):
        raise HTTPException(
            status_code=400,
            detail="operator_email is required and must be a valid RFC 5322 address",
        )
    domain = text.split("@", 1)[1].lower()
    if ".." in domain or domain.endswith("."):
        raise HTTPException(
            status_code=400, detail="operator_email domain is malformed"
        )
    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        raise HTTPException(
            status_code=400, detail="operator_email domain must include a public TLD"
        )
    tld = labels[-1]
    if not tld.isalpha() or len(tld) < 2:
        raise HTTPException(
            status_code=400, detail="operator_email TLD must be alphabetic"
        )
    if tld in _PRIVATE_USE_TLDS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Let's Encrypt rejects private-use TLDs "
                f"(.{tld}). Use a public TLD email such as ops@example.com."
            ),
        )
    return text


@router.get("/openapi/public-https")
def aks_openapi_public_https_status(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the cached public HTTPS endpoint state for the SPA panel.

    Reads from ops Redis only — no kubectl round trip — so polling is
    cheap. ``{enabled: false}`` means the operator has never run the
    setup task (or ran the disable task afterwards).

    The cluster context (``subscription_id`` / ``resource_group`` /
    ``cluster_name``) scopes the lookup to that cluster's per-cluster key
    so a previously-enabled cluster's public FQDN never leaks onto a
    different cluster's API page. The params are optional for backward
    compatibility; when any is missing the legacy global key is read.
    """

    from api.tasks.openapi import get_openapi_public_https_status

    del caller
    return get_openapi_public_https_status(
        subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        resource_group=resource_group,
        cluster_name=cluster_name,
    )


@router.get("/openapi/public-https/operator-email-rules")
def aks_openapi_operator_email_rules(
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Expose the validator rules so the SPA can sync its client gate.

    Single source of truth: the backend's `_validate_operator_email`
    rejects empty / private-TLD emails (otherwise Let's Encrypt rejects
    ACME account registration with `urn:ietf:params:acme:error:invalidContact`).
    The SPA mirrors the rule client-side so the Enable button can be
    disabled without a round trip, but it fetches this list on mount so
    the two sides cannot drift if we later add a new private-use TLD to
    the backend without touching the SPA.
    """
    del caller
    return {
        "private_use_tlds": sorted(_PRIVATE_USE_TLDS),
        "email_regex": _EMAIL_RE.pattern,
        "max_length": 254,
    }


@router.post("/openapi/public-https")
def aks_openapi_public_https_enable(
    body: OpenApiPublicHttpsRequest,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue ``setup_openapi_public_https`` for the given AKS cluster.

    The task is idempotent; re-running it on a cluster that already has
    the public HTTPS path applied refreshes the Ingress + ClusterIssuer
    without burning a Let's Encrypt rate-limit slot (cert-manager reuses
    the existing Certificate Secret when present).
    """

    from api.tasks.openapi import setup_openapi_public_https

    email = _validate_operator_email(body.operator_email)
    LOGGER.info(
        "openapi public-https enable requested cluster=%s caller_oid=%s",
        body.cluster_name,
        redact_oid(caller.object_id),
    )
    result = _safe_delay(
        setup_openapi_public_https,
        subscription_id=body.subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        resource_group=body.resource_group,
        cluster_name=body.cluster_name,
        operator_email=email,
        caller_oid=caller.object_id or "",
    )
    return {
        "id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/public-https/{result.id}/status",
        "status": "queued",
    }


@router.delete("/openapi/public-https")
def aks_openapi_public_https_disable(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Enqueue ``disable_openapi_public_https`` — deletes Ingress + cert.

    ingress-nginx and cert-manager remain installed (cheap, useful for
    other apps + a future re-enable). The cached public base URL is
    cleared so the SPA flips its baseUrl back to the internal LB IP.

    Uses query params (not a JSON body) because some browser / proxy
    combinations strip the body from a `DELETE` request, and the
    SPA's authenticated fetch wrapper only routes JSON bodies through
    POST / PUT.
    """

    from api.tasks.openapi import disable_openapi_public_https

    LOGGER.info(
        "openapi public-https disable requested cluster=%s caller_oid=%s",
        cluster_name,
        redact_oid(caller.object_id),
    )
    result = _safe_delay(
        disable_openapi_public_https,
        subscription_id=subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        resource_group=resource_group,
        cluster_name=cluster_name,
        caller_oid=caller.object_id or "",
    )
    return {
        "id": result.id,
        "task_id": result.id,
        "statusQueryGetUri": f"/api/aks/openapi/public-https/{result.id}/status",
        "status": "queued",
    }


@router.get("/openapi/public-https/{task_id}/status")
def aks_openapi_public_https_task_status(
    task_id: str = Path(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Translate the Celery ``AsyncResult`` into a SPA-friendly envelope.

    Mirrors ``aks_openapi_deploy_status`` shape so the SPA can reuse the
    same polling helper for both flows.
    """

    from celery.result import AsyncResult

    from api.celery_app import celery_app

    del caller
    result = AsyncResult(task_id, app=celery_app)
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
        custom_status.update({"phase": "completed"})
        output = dict(payload)
    else:
        err = ""
        try:
            err = str(result.result or result.info or "")[:500]
        except Exception:
            err = "task failed"
        custom_status.update({"phase": "failed"})
        output = {"status": "failed", "error": err}

    return {
        "task_id": task_id,
        "runtime_status": runtime_status,
        "custom_status": custom_status,
        "output": output,
    }
