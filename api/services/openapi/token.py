"""OpenAPI API token lifecycle helpers.

Responsibility: Generate, read, and apply the sibling OpenAPI API token
Edit boundaries: Keep Kubernetes token storage and runtime cache synchronization here; routes
should only validate HTTP input and shape responses.
Key entry points: `get_openapi_api_token_status`, `ensure_openapi_api_token`
Risky contracts: Never log token values; keep tokens in server-side env/runtime cache and only
return them to authenticated dashboard callers.
Validation: `uv run pytest -q api/tests/test_openapi_token.py`.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.monitoring import _get_k8s_session
from api.services.openapi.runtime import save_openapi_api_token

OPENAPI_DEPLOYMENT_NAME = "elb-openapi"
OPENAPI_CONTAINER_NAME = "openapi"
OPENAPI_TOKEN_ENV = "ELB_OPENAPI_API_TOKEN"  # noqa: S105 - env var name, not a token value.
K8S_NAMESPACE = "default"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenApiTokenError(Exception):
    status_code: int
    code: str
    message: str


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "*" * len(token)
    return f"{token[:4]}{'*' * 12}{token[-6:]}"


def _record_self_heal_audit(
    *,
    event: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    detail: dict[str, Any],
    caller_oid: str = "",
    tenant_id: str = "",
) -> None:
    """Append an audit JobState row for OpenAPI token self-heal events.

    Best-effort by design: the audit append must never block or fail the
    status-read path. Mirrors the ``record_db_op`` pattern so the existing
    ``/api/audit/log`` SPA surface picks the event up automatically.

    ``caller_oid`` / ``tenant_id`` are wired through from the FastAPI
    route's ``require_caller`` dependency so the audit row is owned by the
    user who triggered the GET — without this, ``/api/audit/log`` (which
    queries ``list_for_owner(caller.object_id)``) would never surface the
    event, defeating the "leave a forensic trail" goal. When the trigger
    is an internal path that has no caller (e.g. the OpenAPI proxy
    fallback minting on a 401), the row falls back to
    ``owner_oid="system"`` and is still queryable via direct table reads
    or App Insights.

    The synthetic job_id (``openapi-token:<event>:<cluster>:<ulid>``) is
    prefixed so the audit table groups self-heal events separately from
    BLAST / warmup / DB-ops jobs without a JobState schema change.

    Token values are NEVER passed in ``detail`` — only metadata about the
    cluster + the patch outcome. This keeps the audit row safe to render
    in the SPA and to ship to Log Analytics.
    """
    try:
        import uuid
        from datetime import UTC, datetime

        from api.services.state.job_state import JobState
        from api.services.state_repo import get_state_repo

        now = datetime.now(UTC).isoformat(timespec="seconds")
        job_id = (
            f"openapi-token:{event}:{cluster_name or 'unknown'}:{uuid.uuid4().hex[:12]}"
        )
        payload: dict[str, Any] = {
            "event": event,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
            "ts": now,
        }
        payload.update({k: v for k, v in detail.items() if v is not None})
        status = "failed" if event.endswith("_failed") else "completed"
        repo = get_state_repo()
        repo.create(
            JobState(
                job_id=job_id,
                type="openapi_token_self_heal",
                status=status,
                phase=status,
                owner_oid=caller_oid or "system",
                tenant_id=tenant_id or "",
                created_at=now,
                updated_at=now,
                payload=payload,
            )
        )
        repo.append_history(job_id, event, payload)
    except Exception as exc:
        LOGGER.warning(
            "openapi token self-heal audit append skipped: %s", type(exc).__name__
        )


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _deployment_url(server: str, namespace: str, deployment_name: str) -> str:
    return f"{server}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"


def _read_deployment(
    session: Any,
    server: str,
    namespace: str,
    deployment_name: str,
) -> dict[str, Any]:
    response = session.get(_deployment_url(server, namespace, deployment_name), timeout=10)
    if response.status_code == 404:
        raise OpenApiTokenError(
            404,
            "openapi_deployment_not_found",
            "The elb-openapi deployment was not found in AKS.",
        )
    if response.status_code != 200:
        raise OpenApiTokenError(
            502,
            "openapi_deployment_unavailable",
            f"Kubernetes returned HTTP {response.status_code} while reading elb-openapi.",
        )
    data = response.json()
    return data if isinstance(data, dict) else {}


def _container_env_value(deployment: dict[str, Any], container_name: str, env_name: str) -> str:
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
        or []
    )
    for container in containers:
        if container.get("name") != container_name:
            continue
        for env in container.get("env", []) or []:
            if env.get("name") == env_name and env.get("value"):
                return str(env["value"]).strip()
    return ""


def _patch_deployment_token(
    session: Any,
    server: str,
    *,
    namespace: str,
    deployment_name: str,
    container_name: str,
    token: str,
    deployment: dict[str, Any],
) -> None:
    """Apply a surgical JSON Patch (RFC 6902) to the deployment to set the
    OpenAPI API token env var.

    Strategic-merge-patch on the ``env`` list cannot be used here: if the
    existing entry for ``ELB_OPENAPI_API_TOKEN`` carries a stale
    ``valueFrom`` field (left over from earlier manifest edits or injected
    by an admission webhook), the strategic merge produces an entry with
    BOTH ``value`` and ``valueFrom`` set, which K8s rejects with HTTP 422
    (``env[N].valueFrom`` may not be specified when ``value`` is not
    empty). JSON Patch replaces the whole entry instead of merging
    field-by-field, so the bad ``valueFrom`` is cleared in the same call.
    """
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
        or []
    )
    container_index = -1
    env_index = -1
    for idx, container in enumerate(containers):
        if container.get("name") == container_name:
            container_index = idx
            for env_idx, env in enumerate(container.get("env", []) or []):
                if env.get("name") == OPENAPI_TOKEN_ENV:
                    env_index = env_idx
                    break
            break
    if container_index < 0:
        raise OpenApiTokenError(
            502,
            "openapi_container_not_found",
            (
                f"Container '{container_name}' not found in the elb-openapi "
                "deployment; cannot patch the API token."
            ),
        )

    ops: list[dict[str, Any]] = []
    # The base template may not carry an `annotations` map; create it first
    # so the per-key add below cannot 422 with "path not found".
    template_meta = (
        deployment.get("spec", {}).get("template", {}).get("metadata", {}) or {}
    )
    if "annotations" not in template_meta or template_meta["annotations"] is None:
        ops.append(
            {
                "op": "add",
                "path": "/spec/template/metadata/annotations",
                "value": {},
            }
        )
    ops.append(
        {
            "op": "add",
            "path": (
                "/spec/template/metadata/annotations/"
                "elb-dashboard~1openapi-api-token-rotated-at"
            ),
            "value": _now_iso(),
        }
    )
    env_entry = {"name": OPENAPI_TOKEN_ENV, "value": token}
    if env_index >= 0:
        ops.append(
            {
                "op": "replace",
                "path": f"/spec/template/spec/containers/{container_index}/env/{env_index}",
                "value": env_entry,
            }
        )
    else:
        # Ensure /env exists before appending. K8s rejects "add /env/-" when
        # the path is missing, so guard with an "add /env []" first.
        env_list = containers[container_index].get("env")
        if env_list is None:
            ops.append(
                {
                    "op": "add",
                    "path": f"/spec/template/spec/containers/{container_index}/env",
                    "value": [],
                }
            )
        ops.append(
            {
                "op": "add",
                "path": f"/spec/template/spec/containers/{container_index}/env/-",
                "value": env_entry,
            }
        )

    response = session.patch(
        _deployment_url(server, namespace, deployment_name),
        json=ops,
        headers={"Content-Type": "application/json-patch+json"},
        timeout=15,
    )
    if response.status_code == 404:
        raise OpenApiTokenError(
            404,
            "openapi_deployment_not_found",
            "The elb-openapi deployment was not found in AKS.",
        )
    if response.status_code not in {200, 201, 202}:
        upstream_detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                upstream_detail = str(payload.get("message") or payload.get("reason") or "")
        except Exception:
            upstream_detail = response.text[:300] if response.text else ""
        suffix = f": {upstream_detail}" if upstream_detail else ""
        raise OpenApiTokenError(
            502,
            "openapi_token_patch_failed",
            f"Kubernetes returned HTTP {response.status_code} while updating the API token{suffix}",
        )


def _status_payload(
    *,
    token: str,
    source: str,
    updated_at: str | None = None,
    generated: bool = False,
    rotated: bool = False,
    self_heal_error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "configured": bool(token),
        "token": token,
        "masked_token": _mask_token(token),
        "header_name": "X-ELB-API-Token",
        "env_name": OPENAPI_TOKEN_ENV,
        "source": source,
        "updated_at": updated_at,
        "generated": generated,
        "rotated": rotated,
        # Populated only when GET /openapi/token tried to self-heal a
        # legacy deployment (missing ELB_OPENAPI_API_TOKEN env entry)
        # and the patch failed. The SPA panel renders this as a red
        # banner so the operator immediately sees "Auto-recovery failed:
        # <code> — <message>" instead of the silent "No API token
        # generated" placeholder. `None` for the happy path so consumers
        # can treat it as an optional discriminator.
        "self_heal_error": self_heal_error,
    }


def _sync_runtime_token(token: str, metadata: dict[str, Any]) -> None:
    if not token:
        return
    os.environ[OPENAPI_TOKEN_ENV] = token
    save_openapi_api_token(token, metadata=metadata)
    # Invalidate caches that may hold a stale token / 401 negative entry.
    # Without this the external jobs list keeps returning a cached 401 for
    # up to 30 s after rotation, and the openapi-client-kwargs cache keeps
    # serving the old token for up to 70 s. Both make the BLAST Jobs page
    # appear broken right after the user clicks "Rotate".
    try:
        from api.services.blast.external_jobs import _reset_external_jobs_cache

        _reset_external_jobs_cache()
    except Exception as exc:
        # Cache reset is best-effort — never block token rotation.
        LOGGER.debug("openapi token cache reset skipped: %s", exc)


def get_openapi_api_token_status(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str = K8S_NAMESPACE,
    caller_oid: str = "",
    tenant_id: str = "",
) -> dict[str, Any]:
    """Return the current OpenAPI API token status and value.

    Self-heals legacy deployments created before the deploy-side auto-mint
    (commit 9d4e549): when the elb-openapi deployment exists but its
    ``ELB_OPENAPI_API_TOKEN`` env entry is missing — the pre-fix manifest
    omitted the entry when no token was cached — mint + patch one in place
    so the SPA's API Reference panel does not stay on "No API token
    generated" until the operator clicks "Generate". The mint is
    equivalent to the user pressing Generate, idempotent (only triggers
    when env is empty), and uses the same `_patch_deployment_token` path
    as `ensure_openapi_api_token(regenerate=False)`.

    ``caller_oid`` / ``tenant_id`` are wired through to the audit row so
    ``/api/audit/log`` (which queries ``list_for_owner(caller.object_id)``)
    can surface the self-heal event to the user who triggered it. The
    route passes them via ``Depends(require_caller)``; internal callers
    (proxy fallback) leave them empty and the audit row falls back to
    ``owner_oid="system"``.
    """

    session, server = _get_k8s_session(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=True,
    )
    self_heal_error: dict[str, str] | None = None
    try:
        deployment = _read_deployment(session, server, namespace, OPENAPI_DEPLOYMENT_NAME)
        token = _container_env_value(deployment, OPENAPI_CONTAINER_NAME, OPENAPI_TOKEN_ENV)
        generated = False
        updated_at: str | None = None
        if not token:
            new_token = _generate_token()
            try:
                _patch_deployment_token(
                    session,
                    server,
                    namespace=namespace,
                    deployment_name=OPENAPI_DEPLOYMENT_NAME,
                    container_name=OPENAPI_CONTAINER_NAME,
                    token=new_token,
                    deployment=deployment,
                )
                token = new_token
                generated = True
                updated_at = _now_iso()
                # Promoted from INFO to WARNING so App Insights /
                # operator alerts can fire on the self-heal event —
                # silent fixes also mean silent regressions in the
                # future. The event is also recorded in the audit log
                # (see _record_self_heal_audit below) for forensic
                # traceability.
                LOGGER.warning(
                    "openapi token self-healed: legacy deployment had no "
                    "ELB_OPENAPI_API_TOKEN env entry; minted + patched "
                    "cluster=%s rg=%s sub=%s",
                    cluster_name,
                    resource_group,
                    subscription_id,
                )
                _record_self_heal_audit(
                    event="openapi_token_self_healed",
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                    detail={
                        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
                        "namespace": namespace,
                        "updated_at": updated_at,
                    },
                    caller_oid=caller_oid,
                    tenant_id=tenant_id,
                )
            except OpenApiTokenError as patch_exc:
                # Patch failed (RBAC / webhook / 422). Keep the empty
                # token in the response so the SPA still renders the
                # Generate button — but ALSO ship the failure code +
                # message in `self_heal_error` so the panel can show a
                # red banner with the actionable reason. Logged at
                # ERROR level so operators can spot the failure in
                # App Insights without depending on the UI surface.
                token = ""
                generated = False
                updated_at = None
                self_heal_error = {
                    "code": patch_exc.code,
                    "message": patch_exc.message,
                }
                LOGGER.error(
                    "openapi token self-heal failed cluster=%s rg=%s code=%s msg=%s",
                    cluster_name,
                    resource_group,
                    patch_exc.code,
                    patch_exc.message,
                )
                _record_self_heal_audit(
                    event="openapi_token_self_heal_failed",
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    cluster_name=cluster_name,
                    detail={
                        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
                        "namespace": namespace,
                        "error_code": patch_exc.code,
                        "error_message": patch_exc.message,
                    },
                    caller_oid=caller_oid,
                    tenant_id=tenant_id,
                )
    finally:
        session.close()

    metadata = {
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
        "namespace": namespace,
        "generated": generated,
    }
    _sync_runtime_token(token, metadata)
    return _status_payload(
        token=token,
        source="deployment_env",
        updated_at=updated_at,
        generated=generated,
        self_heal_error=self_heal_error,
    )


def ensure_openapi_api_token(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    regenerate: bool,
    namespace: str = K8S_NAMESPACE,
) -> dict[str, Any]:
    """Create or rotate the OpenAPI API token on the AKS deployment."""

    session, server = _get_k8s_session(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=True,
    )
    try:
        deployment = _read_deployment(session, server, namespace, OPENAPI_DEPLOYMENT_NAME)
        existing = _container_env_value(deployment, OPENAPI_CONTAINER_NAME, OPENAPI_TOKEN_ENV)
        if existing and not regenerate:
            token = existing
            generated = False
            rotated = False
            updated_at = None
        else:
            token = _generate_token()
            _patch_deployment_token(
                session,
                server,
                namespace=namespace,
                deployment_name=OPENAPI_DEPLOYMENT_NAME,
                container_name=OPENAPI_CONTAINER_NAME,
                token=token,
                deployment=deployment,
            )
            generated = not existing
            rotated = bool(existing)
            updated_at = _now_iso()
    finally:
        session.close()

    metadata = {
        "subscription_id": subscription_id,
        "resource_group": resource_group,
        "cluster_name": cluster_name,
        "deployment_name": OPENAPI_DEPLOYMENT_NAME,
        "namespace": namespace,
        "rotated": rotated,
        "generated": generated,
    }
    _sync_runtime_token(token, metadata)
    return _status_payload(
        token=token,
        source="deployment_env",
        updated_at=updated_at,
        generated=generated,
        rotated=rotated,
    )
