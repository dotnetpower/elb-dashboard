"""Authorization helper for upgrade-mutating endpoints.

Module summary: Wraps `require_caller` with an `UpgradeAdmin` check used
by the routes that can change Container App state (`/start`, `/rollback`,
build-log streaming). A caller is permitted when ANY of these holds:
  1. they hold a write Azure RBAC role (Owner / Contributor / …) at the
     platform scope (subscription + resource group) — the primary path, so
     no separate group/allowlist is needed;
  2. their MSAL token carries the `UpgradeAdmin` app role;
  3. their object id is in the env allowlist `UPGRADE_ADMIN_OIDS` (break-glass).

Responsibility: Single-purpose admin gate for upgrade routes.
Edit boundaries: Centralised here so future PRs can add app-role plumbing
  without touching the routes. The RBAC enumeration itself lives in
  `api.services.me_permissions`; this module only consumes its verdict.
Key entry points: `require_upgrade_admin`, `is_upgrade_admin`,
  `caller_has_platform_write`, `UPGRADE_ADMIN_ROLE`, `UPGRADE_ADMIN_OIDS_ENV`.
Risky contracts: `AUTH_DEV_BYPASS` bypasses the bearer check upstream but
  the synthetic identity still has to pass this gate; tests that exercise
  admin routes set `UPGRADE_ADMIN_OIDS` to include the dev-bypass oid. The
  RBAC path degrades CLOSED (enumeration failure → not admin) because this
  is a real authorization gate, unlike the SPA's UX-affordance permissions
  surface which degrades open.
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py
  api/tests/test_persona_matrix.py`.
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException, status

from api.auth import DEV_BYPASS_OID, CallerIdentity, require_caller

LOGGER = logging.getLogger(__name__)

UPGRADE_ADMIN_ROLE = "UpgradeAdmin"
UPGRADE_ADMIN_OIDS_ENV = "UPGRADE_ADMIN_OIDS"


def _allowed_oids() -> set[str]:
    raw = os.environ.get(UPGRADE_ADMIN_OIDS_ENV, "").strip()
    if not raw:
        return set()
    return {oid.strip() for oid in raw.split(",") if oid.strip()}


def caller_has_platform_write(caller: CallerIdentity) -> bool:
    """Return True when ``caller`` holds a write RBAC role at the platform scope.

    "Write role" = Owner / Contributor (and the AKS write roles) at the
    deployment's subscription + resource group, as classified by
    `api.services.me_permissions.compute_caller_permissions` (``can_write``).
    Group-inherited assignments count, because the underlying enumeration uses
    the ``assignedTo()`` OData filter.

    SECURITY — degrades CLOSED. ``compute_caller_permissions`` opens every
    capability when role enumeration fails (a UX affordance so the SPA does not
    grey out buttons on a transient ARM hiccup). For an actual deploy gate we
    must NOT inherit that behaviour: a caller whose enumeration failed, or who
    matched no write role, is treated as NOT an admin here (``can_write and not
    degraded``). The platform scope is read from ``AZURE_SUBSCRIPTION_ID`` /
    ``AZURE_RESOURCE_GROUP``; when the subscription is unset (e.g. unit tests)
    this returns False without any network call.
    """
    if not caller or not caller.object_id:
        return False
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not subscription_id:
        return False
    resource_group = os.environ.get("AZURE_RESOURCE_GROUP", "").strip() or None
    try:
        from api.services import get_credential
        from api.services.me_permissions import compute_caller_permissions

        perms = compute_caller_permissions(
            get_credential(),
            caller_oid=caller.object_id,
            subscription_id=subscription_id,
            resource_group=resource_group,
        )
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning(
            "upgrade.auth: platform-write check failed: %s", type(exc).__name__
        )
        return False
    return bool(perms.can_write and not perms.degraded)


def is_upgrade_admin(caller: CallerIdentity) -> bool:
    """Return True when the caller is permitted to mutate upgrade state.

    Resolution order (any one grants):
      1. **Azure RBAC write role** (Owner / Contributor) at the platform scope
         — the primary path so operators never need a separate group/allowlist.
      2. **MSAL `UpgradeAdmin` app role** — case-insensitive, so a portal typo
         (`upgradeadmin`, `UPGRADEADMIN`, …) is still recognised.
      3. **`UPGRADE_ADMIN_OIDS` env allowlist** — case-sensitive (GUIDs are
         canonicalised by AAD); a break-glass override.

    Audit P1 #11: in a deployed Container Apps revision (`CONTAINER_APP_NAME`
    set), the `DEV_BYPASS_OID` synthetic identity is rejected outright even if
    `UPGRADE_ADMIN_OIDS` accidentally contains it. The dev-bypass identity is
    for local debugging only; trusting it for the upgrade gate would let a
    stale `AUTH_DEV_BYPASS=true` env import escalate into a Container App image
    swap. Tests that exercise admin routes locally rely on the bypass identity
    but never set `CONTAINER_APP_NAME`.
    """
    if (
        caller.object_id == DEV_BYPASS_OID
        and os.environ.get("CONTAINER_APP_NAME")
    ):
        return False
    # 1. Azure RBAC write role at the platform scope (primary path).
    if caller_has_platform_write(caller):
        return True
    # 2. Explicit MSAL app role.
    roles = caller.claims.get("roles") if isinstance(caller.claims, dict) else None
    if isinstance(roles, list):
        target = UPGRADE_ADMIN_ROLE.casefold()
        for role in roles:
            if isinstance(role, str) and role.casefold() == target:
                return True
    # 3. Explicit oid allowlist (break-glass override).
    if caller.object_id and caller.object_id in _allowed_oids():
        return True
    return False


def require_upgrade_admin(
    caller: CallerIdentity = Depends(require_caller),
) -> CallerIdentity:
    """FastAPI dependency that 403s when the caller is not an upgrade admin."""
    if not is_upgrade_admin(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "caller is not authorised for upgrade actions; requires an "
                "Owner or Contributor role on the deployment (subscription or "
                f"resource group), the '{UPGRADE_ADMIN_ROLE}' app role, or the "
                f"caller oid to be listed in the {UPGRADE_ADMIN_OIDS_ENV} env."
            ),
        )
    return caller
