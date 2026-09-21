"""Preflight RBAC check for AKS cluster create.

Verifies the shared dashboard managed identity (Container App UAMI) has the
role assignments the AKS provisioning task actually needs at submit time.
Without this check the operator clicks "+ Create Cluster", the request
queues to Celery, the worker calls `managed_clusters.begin_create_or_update`,
and ARM rejects with AuthorizationFailed 30-60 seconds later — the same
permission gap could have been surfaced inline next to the existing
SKU / Quota / RG checks.

Responsibility: Resolve the MI principal id and enumerate its sub-scope and
    target-RG role assignments. Compose a single `PreflightCheck` row.
Edit boundaries: Pure read-only ARM probing. Does NOT grant or modify any
    role assignment — only reports.
Key entry points: `aks_create_rbac_check`.
Risky contracts: Returns `status="warn"` (not `fail`) when role enumeration
    itself fails — the caller may lack `Microsoft.Authorization/
    roleAssignments/read` on the relevant scope. ARM is still the ground
    truth; preflight must never block submit on a false negative caused by
    a missing read permission. A matching custom-role name is insufficient:
    its live Actions must include RG-write and role-assignment read/write.
Validation: `uv run pytest -q api/tests/test_rbac_preflight.py`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.aks_availability import PreflightCheck

LOGGER = logging.getLogger(__name__)

# Built-in role definition GUIDs (stable across tenants).
# https://learn.microsoft.com/azure/role-based-access-control/built-in-roles
_ROLE_OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_ROLE_READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
_ROLE_USER_ACCESS_ADMINISTRATOR = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"
_AKS_BOOTSTRAP_ROLE_IDS = {
    "Contributor": _ROLE_CONTRIBUTOR,
    "User Access Administrator": _ROLE_USER_ACCESS_ADMINISTRATOR,
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "AcrPull": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
    "Azure Kubernetes Service Cluster User": "4abbcc35-e782-43d8-92c5-2d3f1bd2253f",
}
_GUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

# Name of the project-specific custom role that grants only sub-scope
# `Microsoft.Resources/subscriptions/resourceGroups/write` so AKS can
# auto-create the MC_* node resource group without granting sub-scope
# Contributor. Defined in `infra/modules/workloadRgCreatorRole.bicep`.
AKS_BOOTSTRAP_ROLE_NAME = "Elb Workload RG Creator"
AKS_BOOTSTRAP_RG_WRITE_ACTION = "Microsoft.Resources/subscriptions/resourceGroups/write"
AKS_BOOTSTRAP_REQUIRED_ACTIONS = frozenset(
    {
        AKS_BOOTSTRAP_RG_WRITE_ACTION,
        "Microsoft.Authorization/roleAssignments/read",
        "Microsoft.Authorization/roleAssignments/write",
    }
)

# Roles that satisfy the cluster-RG requirement (managedClusters/write +
# child resources). Owner and Contributor both grant `*` actions; Reader
# does not.
_RG_WRITE_ROLES = {_ROLE_OWNER, _ROLE_CONTRIBUTOR}

# Roles that satisfy the sub-scope requirement (AKS auto-creates the
# MC_*_<cluster>_<region> node RG, which needs
# `Microsoft.Resources/subscriptions/resourceGroups/write` at sub scope).
_SUB_RG_WRITE_BUILTIN_ROLES = {_ROLE_OWNER, _ROLE_CONTRIBUTOR}

# Roles that grant `Microsoft.Authorization/roleAssignments/write` — needed
# by the `ensuring_rbac` provision step so the worker MI can assign
# AcrPull / Storage Blob Data Contributor to the freshly-created AKS
# kubelet identity. Owner inherits this; Contributor does NOT.
_ROLE_ASSIGNMENT_WRITE_ROLES = {_ROLE_OWNER, _ROLE_USER_ACCESS_ADMINISTRATOR}


def _mi_principal_id() -> str | None:
    """Return the dashboard MI's Entra object id, or None when unknown.

    The Container App template exports `SHARED_IDENTITY_PRINCIPAL_ID` to
    every sidecar (see `infra/modules/containerAppControl.bicep`). In a
    local-dev shell without that env var set the preflight degrades to a
    warn row instead of hard-failing.
    """
    pid = (os.environ.get("SHARED_IDENTITY_PRINCIPAL_ID") or "").strip()
    return pid or None


def _list_role_assignments(
    credential: TokenCredential,
    subscription_id: str,
    principal_id: str,
) -> tuple[
    list[tuple[str, str, str, str | None, str | None]],
    str | None,
]:
    """List role assignments for `principal_id` within `subscription_id`.

    Returns `(rows, error_reason)`. Each row is
    `(role_guid_lower, scope_lower, role_definition_id, condition,
    condition_version)`. When enumeration fails (e.g. caller lacks
    `roleAssignments/read`), returns `([], <short_error>)` so the caller
    can degrade to a warn row.
    """
    try:
        from azure.mgmt.authorization import AuthorizationManagementClient
    except Exception as exc:
        return [], f"azure.mgmt.authorization import failed: {type(exc).__name__}"

    try:
        client = AuthorizationManagementClient(credential, subscription_id)
        rows: list[tuple[str, str, str, str | None, str | None]] = []
        for r in client.role_assignments.list_for_subscription(
            filter=f"principalId eq '{principal_id}'"
        ):
            role_def_id = (getattr(r, "role_definition_id", None) or "").lower()
            role_guid = role_def_id.rsplit("/", 1)[-1]
            scope = (getattr(r, "scope", None) or "").lower()
            rows.append(
                (
                    role_guid,
                    scope,
                    role_def_id,
                    getattr(r, "condition", None),
                    getattr(r, "condition_version", None),
                )
            )
        return rows, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:120]}"


def _resolve_role_definition(
    credential: TokenCredential,
    subscription_id: str,
    role_definition_id: str,
) -> tuple[str | None, set[str]]:
    """Return a role definition's display name and normalized Actions."""
    try:
        from azure.mgmt.authorization import AuthorizationManagementClient
    except Exception:
        return None, set()
    try:
        client = AuthorizationManagementClient(credential, subscription_id)
        # `get_by_id` expects the full ARM resource id.
        definition = client.role_definitions.get_by_id(role_definition_id)
        actions = {
            str(action).lower()
            for permission in (getattr(definition, "permissions", None) or [])
            for action in (getattr(permission, "actions", None) or [])
        }
        return getattr(definition, "role_name", None), actions
    except Exception:
        return None, set()


def aks_bootstrap_assignment_issues(
    condition: str | None,
    condition_version: str | None,
) -> list[str]:
    """Return gaps in the custom role's constrained-delegation assignment."""
    normalized = (condition or "").lower()
    issues: list[str] = []
    if condition_version != "2.0":
        issues.append("conditionVersion must be 2.0")
    if "microsoft.authorization/roleassignments/write" not in normalized:
        issues.append("condition must constrain roleAssignments/write")
    if "foranyofanyvalues:guidequals" not in normalized:
        issues.append("condition must use the role-definition GUID whitelist")
    missing_roles = [
        role_name
        for role_name, role_id in _AKS_BOOTSTRAP_ROLE_IDS.items()
        if role_id not in normalized
    ]
    if missing_roles:
        issues.append("condition must allow required runtime roles: " + ", ".join(missing_roles))
    expected_role_ids = {role_id.lower() for role_id in _AKS_BOOTSTRAP_ROLE_IDS.values()}
    unexpected_role_ids = sorted(set(_GUID_PATTERN.findall(normalized)) - expected_role_ids)
    if unexpected_role_ids:
        issues.append(
            "condition contains unexpected role definition ids: " + ", ".join(unexpected_role_ids)
        )
    if "principaltype" not in normalized or "serviceprincipal" not in normalized:
        issues.append("condition must restrict principalType to ServicePrincipal")
    return issues


def aks_bootstrap_role_missing_actions(actions: set[str]) -> list[str]:
    """Return required custom-role Actions absent from a live definition."""
    normalized = {action.lower() for action in actions}
    return sorted(
        action for action in AKS_BOOTSTRAP_REQUIRED_ACTIONS if action.lower() not in normalized
    )


def aks_create_rbac_check(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    principal_id: str | None = None,
) -> PreflightCheck:
    """Return a single preflight row covering the MI's AKS-create RBAC.

    Two scopes are required:

    1. **Cluster resource group** — Contributor (or Owner). Without this
       the worker cannot call `managedClusters/write` on the cluster
       resource.
    2. **Subscription** — `Microsoft.Resources/subscriptions/
       resourceGroups/write`. AKS auto-creates the `MC_*` node resource
       group at sub scope; without this permission the create returns
       AuthorizationFailed even when the cluster RG grant is correct.

    The check accepts either the project-specific custom role
    `Elb Workload RG Creator` (defined in
    `infra/modules/workloadRgCreatorRole.bicep`) or the broader built-in
    Owner / Contributor at sub scope for the sub-scope requirement, so
    operators who chose sub-scope Contributor for simplicity are not
    flagged as misconfigured.
    """
    sub_scope = f"/subscriptions/{subscription_id}".lower()
    rg_scope = f"{sub_scope}/resourcegroups/{(resource_group or '').lower()}"

    pid = principal_id or _mi_principal_id()
    if not pid:
        return PreflightCheck(
            name="rbac",
            status="warn",
            message=(
                "Cannot verify dashboard managed-identity RBAC: "
                "SHARED_IDENTITY_PRINCIPAL_ID is not set. The provision "
                "task will still fail at submit if Azure RBAC is missing."
            ),
            details={"principal_id": None},
        )

    rows, err = _list_role_assignments(credential, subscription_id, pid)
    if err:
        return PreflightCheck(
            name="rbac",
            status="warn",
            message=(
                "Cannot list role assignments for the dashboard managed "
                f"identity ({err}). Provision will surface any real gap "
                "as AuthorizationFailed at submit time."
            ),
            details={"principal_id": pid, "error": err},
        )

    # ---- Cluster RG scope: any of Owner / Contributor at sub OR RG. ----
    cluster_rg_ok = False
    cluster_rg_via: dict[str, Any] = {}
    for role_guid, scope, _, _, _ in rows:
        if role_guid not in _RG_WRITE_ROLES:
            continue
        if scope == sub_scope or scope == rg_scope:
            cluster_rg_ok = True
            cluster_rg_via = {"role_guid": role_guid, "scope": scope}
            break

    # ---- Sub-scope RG creation: built-in Owner / Contributor, OR the
    # project custom role. The custom role's GUID is not stable across
    # subscriptions (Azure assigns one at create time), so we resolve
    # by display name for any non-built-in role assignment at sub scope.
    sub_rg_write_ok = False
    sub_rg_write_via: dict[str, Any] = {}
    # Whether sub-scope RG-write was satisfied via the project's custom
    # role. The custom role also grants `roleAssignments/read+write`
    # with an ABAC whitelist that includes Contributor + UAA (see
    # `infra/modules/workloadRgCreatorRole.bicep`), so the provision
    # task can self-grant Contributor on the cluster RG immediately
    # after creating it — meaning the cluster-RG requirement is
    # bootstrap-capable even when no per-RG Contributor exists yet.
    custom_role_bootstrap_capable = False
    custom_role_missing_actions: list[str] = []
    custom_role_assignment_issues: list[str] = []
    for role_guid, scope, role_def_id, condition, condition_version in rows:
        if scope != sub_scope:
            continue
        if role_guid in _SUB_RG_WRITE_BUILTIN_ROLES:
            sub_rg_write_ok = True
            sub_rg_write_via = {"role_guid": role_guid, "scope": scope}
            break
        # Custom role — verify its live capabilities, not only its display
        # name. Older deployments used the same name but lacked
        # roleAssignments/read+write, so they could create the RG yet could
        # not self-grant Contributor before AKS create.
        role_name, actions = _resolve_role_definition(
            credential,
            subscription_id,
            role_def_id,
        )
        if role_name and role_name.strip().lower() == AKS_BOOTSTRAP_ROLE_NAME.lower():
            custom_role_missing_actions = aks_bootstrap_role_missing_actions(actions)
            if AKS_BOOTSTRAP_RG_WRITE_ACTION.lower() in actions:
                sub_rg_write_ok = True
                sub_rg_write_via = {"role_name": role_name, "scope": scope}
            custom_role_assignment_issues = aks_bootstrap_assignment_issues(
                condition,
                condition_version,
            )
            custom_role_bootstrap_capable = not (
                custom_role_missing_actions or custom_role_assignment_issues
            )
            if sub_rg_write_ok:
                break

    # If the cluster-RG Contributor grant is missing but the MI holds
    # the `Elb Workload RG Creator` custom role at sub scope, the
    # provision task self-grants Contributor + UAA on the cluster RG
    # right after `ensuring_resource_group` and before
    # `arm_create_or_update` (see `api.tasks.azure.provision`). Treat
    # this as ok so renaming the cluster (which derives a fresh
    # `rg-<base>` RG) does not turn a fully-functional bootstrap path
    # into a hard preflight failure.
    cluster_rg_bootstrap_capable = not cluster_rg_ok and custom_role_bootstrap_capable

    if (cluster_rg_ok or cluster_rg_bootstrap_capable) and sub_rg_write_ok:
        if cluster_rg_ok:
            message = (
                "Dashboard managed identity has Contributor on "
                f"'{resource_group}' and sub-scope RG-write — AKS create "
                "is authorized."
            )
        else:
            message = (
                "Dashboard managed identity will self-grant Contributor "
                f"on '{resource_group}' (via the '{AKS_BOOTSTRAP_ROLE_NAME}' "
                "custom role at subscription scope) before AKS create — "
                "no manual role assignment needed."
            )
        return PreflightCheck(
            name="rbac",
            status="ok",
            message=message,
            details={
                "principal_id": pid,
                "cluster_rg_grant": cluster_rg_via,
                "cluster_rg_bootstrap_capable": cluster_rg_bootstrap_capable,
                "sub_rg_write_grant": sub_rg_write_via,
            },
        )

    missing: list[dict[str, Any]] = []
    if not cluster_rg_ok:
        missing.append(
            {
                "scope": f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
                "role": "Contributor",
                "reason": (
                    "Required for Microsoft.ContainerService/managedClusters/"
                    "write on the cluster resource group."
                ),
                "remediation": (
                    f"az role assignment create --assignee-object-id {pid} "
                    "--assignee-principal-type ServicePrincipal "
                    "--role Contributor "
                    f"--scope /subscriptions/{subscription_id}"
                    f"/resourceGroups/{resource_group}"
                ),
            }
        )
    if not sub_rg_write_ok:
        missing.append(
            {
                "scope": f"/subscriptions/{subscription_id}",
                "role": AKS_BOOTSTRAP_ROLE_NAME,
                "reason": (
                    "AKS auto-creates the MC_<rg>_<cluster>_<region> node "
                    "resource group at subscription scope; the dashboard "
                    "managed identity needs Microsoft.Resources/"
                    "subscriptions/resourceGroups/write to allow that."
                ),
                "remediation": (
                    "Re-run `./deploy.sh` (azd up) so the new "
                    f"'{AKS_BOOTSTRAP_ROLE_NAME}' custom role assignment in "
                    "infra/modules/workloadRgCreatorRole.bicep is applied; "
                    "or grant Contributor at subscription scope manually."
                ),
            }
        )

    summary = (
        f"Dashboard managed identity is missing {len(missing)} role assignment(s) "
        "needed for AKS create."
    )
    if custom_role_missing_actions or custom_role_assignment_issues:
        summary = (
            f"The assigned '{AKS_BOOTSTRAP_ROLE_NAME}' role or its constrained "
            "delegation condition is outdated and cannot safely self-grant "
            "Contributor on the cluster resource group. Re-run azd provision "
            "to update it, or grant Contributor on the target resource group "
            "manually."
        )
    return PreflightCheck(
        name="rbac",
        status="fail",
        message=summary,
        details={
            "principal_id": pid,
            "missing": missing,
            "cluster_rg_bootstrap_capable": cluster_rg_bootstrap_capable,
            "custom_role_missing_actions": custom_role_missing_actions,
            "custom_role_assignment_issues": custom_role_assignment_issues,
        },
    )


def aks_runtime_rbac_check(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    acr_resource_group: str = "",
    acr_name: str = "",
    storage_resource_group: str = "",
    storage_account: str = "",
    principal_id: str | None = None,
) -> PreflightCheck:
    """Verify the dashboard MI can assign runtime RBAC to the kubelet identity.

    After AKS creation, the provision task assigns AcrPull on the platform
    ACR and Storage Blob Data Contributor on the workload Storage account
    to the freshly-minted AKS kubelet managed identity. That write requires
    `Microsoft.Authorization/roleAssignments/write` on the target scope —
    granted by **User Access Administrator** or **Owner**. Contributor
    alone is not enough.

    Returns a single preflight row:

    * `ok`   — UAA (or Owner) covers every required target scope, or there
      is no target resource configured (caller did not pass acr/storage).
    * `warn` — UAA is missing on at least one target scope. The provision
      task will then fail-fast at the `ensuring_rbac` step (see
      `api.tasks.azure.provision_aks`). This row is "warn", not "fail",
      because Azure RBAC is still the ground truth — degraded reads or
      sub-scope grants we cannot see can still make the runtime assignment
      succeed.
    * `warn` — also when principal id is unknown or role enumeration fails
      (mirrors `aks_create_rbac_check`).
    """
    pid = principal_id or _mi_principal_id()
    if not pid:
        return PreflightCheck(
            name="rbac_runtime",
            status="warn",
            message=(
                "Cannot verify dashboard managed-identity runtime RBAC: "
                "SHARED_IDENTITY_PRINCIPAL_ID is not set."
            ),
            details={"principal_id": None},
        )

    # If there is no ACR or Storage target, the runtime RBAC step is a
    # no-op (provision skips it). Report ok rather than warn so the
    # preflight checklist stays clean.
    has_targets = bool(acr_name and acr_resource_group) or bool(
        storage_account and storage_resource_group
    )
    if not has_targets:
        return PreflightCheck(
            name="rbac_runtime",
            status="ok",
            message=(
                "No ACR or Storage targets configured; the runtime RBAC step has nothing to assign."
            ),
            details={"principal_id": pid, "targets": []},
        )

    rows, err = _list_role_assignments(credential, subscription_id, pid)
    if err:
        return PreflightCheck(
            name="rbac_runtime",
            status="warn",
            message=(
                "Cannot list role assignments for the dashboard managed "
                f"identity ({err}). Runtime RBAC will surface any gap as "
                "task failure at submit time."
            ),
            details={"principal_id": pid, "error": err},
        )

    sub_scope = f"/subscriptions/{subscription_id}".lower()
    # Compute the lowercase target scopes we need UAA on. Each scope
    # passes the check if UAA is held at the resource scope itself, the
    # containing RG, or sub scope.
    targets: list[tuple[str, str]] = []
    if acr_name and acr_resource_group:
        targets.append(
            (
                "AcrPull",
                (
                    f"/subscriptions/{subscription_id}"
                    f"/resourceGroups/{acr_resource_group}"
                    f"/providers/Microsoft.ContainerRegistry/registries/{acr_name}"
                ).lower(),
            )
        )
    if storage_account and storage_resource_group:
        targets.append(
            (
                "Storage Blob Data Contributor",
                (
                    f"/subscriptions/{subscription_id}"
                    f"/resourceGroups/{storage_resource_group}"
                    f"/providers/Microsoft.Storage/storageAccounts/{storage_account}"
                ).lower(),
            )
        )

    # Pre-compute (role_guid, scope_lower) pairs for fast covering-scope
    # lookup. UAA at any scope that contains the target satisfies it.
    uaa_grants = [
        (role_guid, scope)
        for role_guid, scope, _, _, _ in rows
        if role_guid in _ROLE_ASSIGNMENT_WRITE_ROLES
    ]

    def _covers(target_scope: str) -> dict[str, Any] | None:
        # Path-component-aware ancestor check. A grant at
        # `/subscriptions/X/resourcegroups/rg` must NOT match a target at
        # `/subscriptions/X/resourcegroups/rg-acr/providers/...` (the two
        # RGs are unrelated; naive `str.startswith` would false-positive).
        # Match rules:
        #   - sub_scope grant covers every scope in the subscription
        #   - exact match on the target itself
        #   - grant is a strict path-segment prefix of target
        #     (target == grant or target startswith grant + "/")
        for role_guid, grant_scope in uaa_grants:
            if grant_scope == sub_scope:
                return {"role_guid": role_guid, "scope": grant_scope}
            if target_scope == grant_scope:
                return {"role_guid": role_guid, "scope": grant_scope}
            if target_scope.startswith(grant_scope + "/"):
                return {"role_guid": role_guid, "scope": grant_scope}
        return None

    missing: list[dict[str, Any]] = []
    via: dict[str, dict[str, Any]] = {}
    for label, target_scope in targets:
        cover = _covers(target_scope)
        if cover is None:
            missing.append(
                {
                    "role_assignment": label,
                    "target_scope": target_scope,
                    "needed_role": "User Access Administrator (or Owner)",
                    "reason": (
                        "Needed by the dashboard managed identity to grant "
                        f"'{label}' to the AKS kubelet identity on this "
                        "scope. Contributor does not include "
                        "Microsoft.Authorization/roleAssignments/write."
                    ),
                    "remediation": (
                        f"az role assignment create --assignee-object-id {pid} "
                        "--assignee-principal-type ServicePrincipal "
                        "--role 'User Access Administrator' "
                        f"--scope {target_scope}"
                    ),
                }
            )
        else:
            via[label] = cover

    if not missing:
        return PreflightCheck(
            name="rbac_runtime",
            status="ok",
            message=(
                "Dashboard managed identity has User Access Administrator "
                "(or Owner) on every runtime RBAC target — AKS kubelet "
                "role assignment will succeed."
            ),
            details={"principal_id": pid, "grants": via},
        )

    return PreflightCheck(
        name="rbac_runtime",
        status="warn",
        message=(
            "Dashboard managed identity is missing User Access "
            f"Administrator on {len(missing)} runtime RBAC target(s). "
            "If Azure does not grant the equivalent at a covering scope "
            "we cannot see, the provision task will fail at the "
            "'Granting role assignments' step."
        ),
        details={
            "principal_id": pid,
            "missing": missing,
            "verified": via,
        },
    )
