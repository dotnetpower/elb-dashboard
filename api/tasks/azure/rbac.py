"""Runtime RBAC helpers for the AKS kubelet identity + `assign_aks_roles` task.

Responsibility: Grant the AKS kubelet identity the runtime roles it needs (`AcrPull`
    on the registry, `Storage Blob Data Contributor` on the workload storage account)
    and expose the same flow as a stand-alone Celery task for the SPA's "Re-assign
    roles" affordance.
Edit boundaries: All AKS-kubelet role-assignment writes belong here. The provision
    task calls `ensure_aks_runtime_rbac`; routes call the `assign_aks_roles` task by
    string name.
Key entry points: `attach_acr`, `grant_storage_blob_contributor_to_aks`,
    `ensure_aks_runtime_rbac`, `assign_aks_roles` (Celery task
    `api.tasks.azure.assign_aks_roles`).
Risky contracts: Task name `api.tasks.azure.assign_aks_roles` must not change — the
    SPA + tests reference it. Role assignment Conflicts / RoleAssignmentExists are
    treated as success; the freshly-created kubelet MI's Entra-ID propagation is
    handled via short retry on `PrincipalNotFound`. Any other failure is recorded
    in `roles_failed` and the caller (`provision_aks`) escalates to task failure.
Validation: `uv run pytest -q api/tests/test_azure_tasks.py
    api/tests/test_warmup_route.py`.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from celery import shared_task

import api.tasks.azure as _facade

LOGGER = logging.getLogger(__name__)


def _resolve_workload_storage_defaults(
    storage_resource_group: str,
    storage_account: str,
) -> tuple[str, str]:
    """Default the workload-Storage RBAC target to the platform's own Storage.

    The SPA's cluster-provision form is allowed to omit `storage_account`
    (it has historically defaulted to `""`); without this fallback the
    downstream `ensure_aks_runtime_rbac` silently skips the Storage Blob
    Data Contributor grant and the kubelet later 403s on `azcopy cp`
    during warmup (`AuthorizationPermissionMismatch`). The dashboard's
    Container App always sets `STORAGE_ACCOUNT_NAME` + `AZURE_RESOURCE_GROUP`
    in the worker env, so when the caller omits the target we fill it
    from there.

    Unit tests that intentionally exercise the "no storage target" path
    do not set the env vars, so the existing skip-when-empty contract is
    preserved for them.
    """
    if storage_account.strip():
        return storage_resource_group, storage_account
    env_account = (
        os.environ.get("AZURE_STORAGE_ACCOUNT")
        or os.environ.get("STORAGE_ACCOUNT_NAME")
        or ""
    ).strip()
    if not env_account:
        return storage_resource_group, storage_account
    env_rg = storage_resource_group.strip() or os.environ.get(
        "AZURE_RESOURCE_GROUP", ""
    ).strip()
    return env_rg, env_account

# PrincipalNotFound retry window — the freshly-created AKS kubelet
# managed identity needs a few seconds for Entra ID to propagate to the
# Authorization service. Documented Azure guidance is "up to ~60 s".
_PRINCIPAL_PROPAGATION_RETRY_SECONDS = 60.0
_PRINCIPAL_PROPAGATION_INITIAL_DELAY = 2.0
_PRINCIPAL_PROPAGATION_MAX_DELAY = 10.0


def _is_idempotent_conflict(exc: BaseException) -> bool:
    msg = str(exc)
    return "RoleAssignmentExists" in msg or "Conflict" in msg


def _is_principal_propagation_error(exc: BaseException) -> bool:
    # Case-insensitive across the whole message — Azure sometimes phrases
    # the error as "PrincipalNotFound" (code), "does not exist in the
    # directory" (longer message), or "principalId ... was not found".
    msg = str(exc).lower()
    return (
        "principalnotfound" in msg
        or "does not exist in the directory" in msg
        or ("principalid" in msg and "not found" in msg)
    )


def _create_role_assignment_with_retry(
    auth_cl: Any,
    scope: str,
    role_assignment_name: str,
    parameters: Any,
    *,
    label: str,
) -> None:
    """Wrap `role_assignments.create` with idempotency + Entra propagation retry.

    Raises the original Azure SDK exception on any non-recoverable error so
    the caller can record it in `roles_failed` and fail the provision task.
    Treats `RoleAssignmentExists` / `Conflict` as success. Retries with
    exponential backoff (capped) when the service reports
    `PrincipalNotFound` — the kubelet MI was just minted by the AKS
    create_or_update poller a few seconds ago.
    """

    deadline = time.monotonic() + _PRINCIPAL_PROPAGATION_RETRY_SECONDS
    delay = _PRINCIPAL_PROPAGATION_INITIAL_DELAY
    while True:
        try:
            auth_cl.role_assignments.create(
                scope=scope,
                role_assignment_name=role_assignment_name,
                parameters=parameters,
            )
            return
        except Exception as exc:
            if _is_idempotent_conflict(exc):
                LOGGER.info("%s role already assigned (idempotent)", label)
                return
            if _is_principal_propagation_error(exc) and time.monotonic() < deadline:
                LOGGER.info(
                    "%s assignment hit PrincipalNotFound (Entra propagation), "
                    "retrying in %.1fs",
                    label,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, _PRINCIPAL_PROPAGATION_MAX_DELAY)
                continue
            raise


# Tests monkeypatch `api.tasks.azure.aks_client` / `acr_client` / `storage_client` /
# `get_credential` / `_attach_acr` / `_grant_storage_blob_contributor_to_aks`. Look
# the symbols up on the package at call time so those patches take effect here.
def aks_client(cred: Any, subscription_id: str) -> Any:
    return _facade.aks_client(cred, subscription_id)


def acr_client(cred: Any, subscription_id: str) -> Any:
    return _facade.acr_client(cred, subscription_id)


def storage_client(cred: Any, subscription_id: str) -> Any:
    return _facade.storage_client(cred, subscription_id)


def get_credential() -> Any:
    return _facade.get_credential()


def _resolve_kubelet_oid(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> str | None:
    """Read the kubelet managed-identity `object_id` off the AKS cluster.

    Returns `None` when the cluster has no kubelet identity profile (very
    old AKS or service-principal mode). Callers should treat `None` as a
    silent skip — there is no kubelet MI to grant roles to.
    """
    aks_cl = aks_client(cred, subscription_id)
    cluster = aks_cl.managed_clusters.get(resource_group, cluster_name)
    if cluster.identity_profile and "kubeletidentity" in cluster.identity_profile:
        return cluster.identity_profile["kubeletidentity"].object_id
    return None


def attach_acr(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_resource_group: str,
    acr_name: str,
    *,
    kubelet_oid: str | None = None,
) -> None:
    """Grant AcrPull to the AKS kubelet identity on the ACR.

    Accepts an optional pre-resolved `kubelet_oid` so callers in a tight
    loop (provision_aks) can avoid the duplicate `managed_clusters.get`
    round trip. When omitted, falls back to the legacy lookup so external
    callers / tests keep working.
    """
    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    from api.services.azure_clients import authorization_client

    if kubelet_oid is None:
        kubelet_oid = _resolve_kubelet_oid(
            cred, subscription_id, resource_group, cluster_name
        )

    if not kubelet_oid:
        LOGGER.warning("No kubelet identity found, skipping ACR attach")
        return

    acr_cl = acr_client(cred, subscription_id)
    registry = acr_cl.registries.get(acr_resource_group, acr_name)
    acr_scope = registry.id

    # AcrPull role definition ID (well-known)
    acr_pull_role = "7f951dda-4ed3-4680-a7ca-43fe172d538d"

    auth_cl = authorization_client(cred, subscription_id)
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{acr_pull_role}"
    )
    role_assignment_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{acr_scope}|{kubelet_oid}|{acr_pull_role}")
    )
    _create_role_assignment_with_retry(
        auth_cl,
        acr_scope,
        role_assignment_id,
        RoleAssignmentCreateParameters(  # type: ignore[call-arg]
            role_definition_id=role_definition_id,
            principal_id=kubelet_oid,
            principal_type="ServicePrincipal",
        ),
        label="AcrPull",
    )
    LOGGER.info("AcrPull role assigned to %s on %s", kubelet_oid, acr_name)


def grant_storage_blob_contributor_to_aks(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_resource_group: str,
    storage_account: str,
    *,
    kubelet_oid: str | None = None,
) -> None:
    """Grant Storage Blob Data Contributor to the AKS kubelet identity.

    Accepts an optional pre-resolved `kubelet_oid`; falls back to the
    cluster lookup when not provided.
    """
    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    from api.services.azure_clients import authorization_client

    if kubelet_oid is None:
        kubelet_oid = _resolve_kubelet_oid(
            cred, subscription_id, resource_group, cluster_name
        )

    if not kubelet_oid:
        LOGGER.warning("No kubelet identity found, skipping Storage Blob Data Contributor")
        return

    storage = storage_client(cred, subscription_id).storage_accounts.get_properties(
        storage_resource_group,
        storage_account,
    )
    storage_scope = storage.id
    blob_contributor_role = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"

    auth_cl = authorization_client(cred, subscription_id)
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{blob_contributor_role}"
    )
    role_assignment_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{storage_scope}|{kubelet_oid}|{blob_contributor_role}")
    )
    _create_role_assignment_with_retry(
        auth_cl,
        storage_scope,
        role_assignment_id,
        RoleAssignmentCreateParameters(  # type: ignore[call-arg]
            role_definition_id=role_definition_id,
            principal_id=kubelet_oid,
            principal_type="ServicePrincipal",
        ),
        label="Storage Blob Data Contributor",
    )
    LOGGER.info(
        "Storage Blob Data Contributor role assigned to %s on %s",
        kubelet_oid,
        storage_account,
    )


# Network Contributor (well-known). Grants subnets/join + read/write so an
# AKS cluster created in a BYO subnet can attach nodes and provision internal
# LoadBalancer frontend IPs in that subnet.
_NETWORK_CONTRIBUTOR_ROLE = "4d97b98b-1d4f-4787-a291-c67834d212e7"


def grant_network_contributor_on_subnet(
    cred: Any,
    subscription_id: str,
    *,
    principal_id: str,
    subnet_id: str,
    label: str = "AKS cluster identity",
) -> None:
    """Grant Network Contributor on a single subnet to a principal.

    Used for BYO-subnet AKS: the cluster's control-plane managed identity
    needs Network Contributor on the hub `snet-aks` subnet so the Azure
    cloud-provider can create the `elb-openapi` internal LoadBalancer's
    frontend IP in that subnet. Node attachment itself is authorised at
    create time by the requesting identity (the dashboard MI, which holds
    Network Contributor on the platform RG), but the *runtime* LB
    reconcile runs as the cluster identity — without this grant the
    internal LB Service stays `<pending>` with AuthorizationFailed.

    Idempotent (stable assignment UUID) and tolerant of Entra propagation
    via `_create_role_assignment_with_retry`. No-ops on empty inputs.
    """
    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    from api.services.azure_clients import authorization_client

    if not principal_id or not subnet_id:
        LOGGER.info(
            "grant_network_contributor_on_subnet: missing principal_id/subnet_id, skipping"
        )
        return

    auth_cl = authorization_client(cred, subscription_id)
    role_definition_id = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
        f"roleDefinitions/{_NETWORK_CONTRIBUTOR_ROLE}"
    )
    role_assignment_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{subnet_id}|{principal_id}|{_NETWORK_CONTRIBUTOR_ROLE}",
        )
    )
    _create_role_assignment_with_retry(
        auth_cl,
        subnet_id,
        role_assignment_id,
        RoleAssignmentCreateParameters(  # type: ignore[call-arg]
            role_definition_id=role_definition_id,
            principal_id=principal_id,
            principal_type="ServicePrincipal",
        ),
        label=f"Network Contributor ({label})",
    )
    LOGGER.info(
        "Network Contributor assigned to %s on subnet %s", principal_id, subnet_id
    )



def _call_with_optional_kubelet_oid(
    fn: Any,
    *positional: Any,
    kubelet_oid: str | None,
) -> None:
    """Call `fn(*positional, kubelet_oid=kubelet_oid)` with a fallback that
    handles test fakes / older monkeypatches that don't accept `kubelet_oid`.

    Only ``TypeError`` whose message mentions ``kubelet_oid`` triggers the
    fallback — a genuine TypeError from inside the callee (e.g. bad SDK
    parameters) still propagates. Without that narrowing, a legitimate
    bug in the real role-assignment code would be silently retried as if
    it were a signature mismatch.
    """
    try:
        fn(*positional, kubelet_oid=kubelet_oid)
    except TypeError as exc:
        if "kubelet_oid" not in str(exc):
            raise
        fn(*positional)


# Built-in role definition GUIDs used by the dashboard-MI self-grant on the
# AKS cluster RG. Kept here (rather than imported from openapi.constants) so
# this module stays self-contained for unit tests that stub the openapi
# package.
_ROLE_CONTRIBUTOR_GUID = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_ROLE_USER_ACCESS_ADMINISTRATOR_GUID = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"


def _dashboard_mi_recovery_command(
    *,
    subscription_id: str,
    cluster_resource_group: str,
    mi_principal_id: str,
) -> str:
    """Render the exact `grant-runtime-rbac.sh` command that closes the gap.

    Surfaced verbatim in the provision_aks completion payload so a tenant
    admin can copy-paste it without guessing the principal id or scope.
    """
    return (
        "bash scripts/dev/grant-runtime-rbac.sh --yes "
        f"--cluster-rg {cluster_resource_group} "
        f"--principal-id {mi_principal_id} "
        f"--subscription {subscription_id}"
    )


def ensure_dashboard_mi_cluster_rg_roles(
    cred: Any,
    *,
    subscription_id: str,
    cluster_resource_group: str,
    mi_principal_id: str = "",
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Self-grant Contributor + User Access Administrator to the dashboard MI
    on the AKS cluster RG.

    Without these two roles on `rg-elb-cluster`, the downstream
    `api.tasks.openapi.rbac.setup_workload_identity` step (triggered when
    the operator later clicks "Deploy elb-openapi") fails immediately
    because the worker MI cannot create `id-elb-openapi`, its federated
    credential, or assign Contributor / Storage Blob Data Contributor /
    AKS Cluster User to it.

    Best-effort by design:

    * `mi_principal_id` empty (local-dev shell with no
      `SHARED_IDENTITY_PRINCIPAL_ID`) → returns ``{"skipped": True}``.
    * Self-grant succeeds (or the role assignment already exists) → row
      goes into ``roles_assigned``.
    * Self-grant fails (typically `AuthorizationFailed` because the MI
      lacks `Microsoft.Authorization/roleAssignments/write` on the
      cluster RG — pre-`workloadRgCreatorRole` ABAC condition) → row
      goes into ``roles_failed`` with the short error message. The
      caller (`provision_aks`) does **not** fail the task: the cluster
      is fully usable for everything except OpenAPI deploy, and the
      summary embeds an exact `grant-runtime-rbac.sh` command the
      operator can paste.

    Returns ``{"roles_assigned": [...], "roles_failed": {role: error},
    "mi_principal_id": <oid>, "cluster_resource_group": <rg>,
    "recovery_command": <str>}``. ``recovery_command`` is included on
    every call (even successful ones) so the SPA can render a "Re-run
    if needed" affordance.
    """

    principal_id = (mi_principal_id or "").strip()
    if not principal_id:
        principal_id = (os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID") or "").strip()
    if not principal_id:
        return {
            "skipped": True,
            "reason": "SHARED_IDENTITY_PRINCIPAL_ID not set",
            "roles_assigned": [],
            "roles_failed": {},
        }

    from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

    from api.services.azure_clients import authorization_client

    scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{cluster_resource_group}"
    )
    auth_cl = authorization_client(cred, subscription_id)

    targets: list[tuple[str, str]] = [
        ("Contributor", _ROLE_CONTRIBUTOR_GUID),
        ("User Access Administrator", _ROLE_USER_ACCESS_ADMINISTRATOR_GUID),
    ]

    roles_assigned: list[str] = []
    roles_failed: dict[str, str] = {}

    for label, role_guid in targets:
        if progress_callback is not None:
            progress_callback(
                "ensuring_dashboard_mi_rbac",
                f"Self-granting {label} on {cluster_resource_group}",
            )
        role_definition_id = (
            f"/subscriptions/{subscription_id}/providers/"
            f"Microsoft.Authorization/roleDefinitions/{role_guid}"
        )
        # Stable assignment id so re-runs hit the idempotent
        # `RoleAssignmentExists` branch instead of leaving duplicates.
        assignment_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal_id}|{role_guid}")
        )
        try:
            _create_role_assignment_with_retry(
                auth_cl,
                scope,
                assignment_id,
                RoleAssignmentCreateParameters(  # type: ignore[call-arg]
                    role_definition_id=role_definition_id,
                    principal_id=principal_id,
                    principal_type="ServicePrincipal",
                ),
                label=f"dashboard-MI {label}",
            )
            roles_assigned.append(label)
        except Exception as exc:
            # Most common: AuthorizationFailed because the MI lacks
            # `Microsoft.Authorization/roleAssignments/write` at this
            # scope (pre-Part-C deployments where the
            # `Elb Workload RG Creator` custom role does not yet allow
            # role-assignment writes). Record + continue so the second
            # role is still attempted and the cluster create is not
            # marked failed.
            LOGGER.warning(
                "dashboard-MI self-grant %s on %s failed: %s",
                label,
                cluster_resource_group,
                str(exc)[:200],
            )
            roles_failed[label] = str(exc)[:300]

    return {
        "roles_assigned": roles_assigned,
        "roles_failed": roles_failed,
        "mi_principal_id": principal_id,
        "cluster_resource_group": cluster_resource_group,
        "recovery_command": _dashboard_mi_recovery_command(
            subscription_id=subscription_id,
            cluster_resource_group=cluster_resource_group,
            mi_principal_id=principal_id,
        ),
    }


def ensure_aks_runtime_rbac(
    cred: Any,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Best-effort runtime RBAC ensure for the AKS kubelet identity.

    Returns ``{"roles_assigned": [...], "roles_failed": {role: error, ...}}``.
    Callers (currently ``provision_aks`` and the ``assign_aks_roles`` task)
    are expected to treat a non-empty ``roles_failed`` as a hard failure —
    a cluster whose kubelet cannot pull from ACR or read from Storage will
    silently break BLAST submits. Idempotent role-exists conflicts and
    transient ``PrincipalNotFound`` (Entra propagation) are absorbed by
    `_create_role_assignment_with_retry` and never surface here.

    The optional ``progress_callback(phase, message)`` lets the provision
    task publish per-role sub-phases (``ensuring_rbac_acr`` /
    ``ensuring_rbac_storage``) so the UI banner can show what is currently
    being granted instead of one long "Granting role assignments" pause.
    """
    roles_assigned: list[str] = []
    roles_failed: dict[str, str] = {}

    # Default the workload-Storage target to the platform's own Storage
    # when the caller passes empty strings. This is the safety net for
    # the SPA cluster-provision form omitting `storage_account`; without
    # it the kubelet ends up with `AcrPull` only and `azcopy cp` later
    # 403s on `blast-db/*.manifest` during warmup.
    storage_resource_group, storage_account = _resolve_workload_storage_defaults(
        storage_resource_group, storage_account
    )

    # Single kubelet OID lookup shared by both downstream grants. Without
    # this, attach_acr + grant_storage_blob_contributor_to_aks each did
    # their own `managed_clusters.get` round trip (~1-3 s each).
    kubelet_lookup_error: str | None = None
    try:
        kubelet_oid = _resolve_kubelet_oid(
            cred, subscription_id, resource_group, cluster_name
        )
    except Exception as exc:
        LOGGER.warning("Kubelet OID lookup failed: %s", type(exc).__name__)
        kubelet_oid = None
        kubelet_lookup_error = f"{type(exc).__name__}: {str(exc)[:200]}"

    # Pre-flight: if there is at least one runtime-RBAC target but no
    # kubelet identity, every downstream grant becomes a silent skip
    # (`if not kubelet_oid: return` inside attach_acr / grant_storage_*).
    # Record an explicit failure so `provision_aks` fail-fasts instead of
    # marking the cluster "Cluster ready" with no roles assigned — which
    # was the historical silent-failure mode for clusters created without
    # a kubelet managed identity (e.g. legacy service-principal mode or
    # an interrupted create).
    has_targets = bool(acr_name and acr_resource_group) or bool(
        storage_account and storage_resource_group
    )
    if has_targets and not kubelet_oid:
        message = (
            kubelet_lookup_error
            or "AKS cluster has no kubelet managed identity "
            "(identity_profile.kubeletidentity is missing). The cluster "
            "may be using service-principal mode or the create was "
            "interrupted before the kubelet MI was provisioned."
        )
        if acr_name and acr_resource_group:
            roles_failed["AcrPull"] = message
        if storage_account and storage_resource_group:
            roles_failed["Storage Blob Data Contributor"] = message
        return {
            "cluster_name": cluster_name,
            "roles_assigned": roles_assigned,
            "roles_failed": roles_failed,
        }

    if acr_name and acr_resource_group:
        if progress_callback is not None:
            progress_callback(
                "ensuring_rbac_acr",
                f"Granting AcrPull to AKS kubelet on {acr_name}",
            )
        try:
            _call_with_optional_kubelet_oid(
                _facade._attach_acr,
                cred,
                subscription_id,
                resource_group,
                cluster_name,
                acr_resource_group,
                acr_name,
                kubelet_oid=kubelet_oid,
            )
            roles_assigned.append("AcrPull")
        except Exception as exc:
            LOGGER.warning("AcrPull assignment failed: %s", exc)
            roles_failed["AcrPull"] = str(exc)[:300]

    if storage_account and storage_resource_group:
        if progress_callback is not None:
            progress_callback(
                "ensuring_rbac_storage",
                f"Granting Storage Blob Data Contributor on {storage_account}",
            )
        try:
            _call_with_optional_kubelet_oid(
                _facade._grant_storage_blob_contributor_to_aks,
                cred,
                subscription_id,
                resource_group,
                cluster_name,
                storage_resource_group,
                storage_account,
                kubelet_oid=kubelet_oid,
            )
            roles_assigned.append("Storage Blob Data Contributor")
        except Exception as exc:
            LOGGER.warning("Storage Blob Data Contributor assignment failed: %s", exc)
            roles_failed["Storage Blob Data Contributor"] = str(exc)[:300]

    return {
        "cluster_name": cluster_name,
        "roles_assigned": roles_assigned,
        "roles_failed": roles_failed,
    }


@shared_task(name="api.tasks.azure.assign_aks_roles", bind=True, max_retries=2)
def assign_aks_roles(
    self: Any,
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
) -> dict[str, Any]:
    """Assign runtime RBAC roles to the AKS kubelet identity.

    Returns the role-assignment summary with ``status: completed`` when
    every requested role landed. When at least one role failed (caller
    lacks UAA on the target scope, kubelet identity vanished, etc.) the
    task raises ``RuntimeError`` so Celery marks the result as ``FAILURE``
    instead of "completed with a quiet roles_failed[] dict" — that
    silent-success mode is what made the "re-assign roles" button look
    like a success while the kubelet was still unable to pull from ACR.
    """
    cred = _facade.get_credential()
    summary = _facade._ensure_aks_runtime_rbac(
        cred,
        subscription_id,
        resource_group,
        cluster_name,
        acr_resource_group=acr_resource_group,
        acr_name=acr_name,
        # Do not fall back to the AKS cluster RG — see provision_aks for
        # the rationale. `ensure_aks_runtime_rbac` does its own env-based
        # default when this is empty.
        storage_resource_group=storage_resource_group,
        storage_account=storage_account,
    )
    failed = summary.get("roles_failed") or {}
    if failed:
        if isinstance(failed, dict):
            failed_items = ", ".join(f"{r}: {e}" for r, e in failed.items())
        else:
            failed_items = ", ".join(str(r) for r in failed)
        raise RuntimeError(
            f"Failed to assign runtime RBAC: {failed_items}. The dashboard "
            "managed identity may be missing User Access Administrator on "
            "the ACR / Storage scopes."
        )
    return {**summary, "status": "completed"}
