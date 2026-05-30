"""Authorization helper for upgrade-mutating endpoints.

Module summary: Wraps `require_caller` with an `UpgradeAdmin` check used
by the routes that can change Container App state (`/start`, `/rollback`,
build-log streaming). The admin signal is either an MSAL `roles` claim
("UpgradeAdmin") or the caller's object id appearing in the env-supplied
allowlist `UPGRADE_ADMIN_OIDS`. Either source counts.

Responsibility: Single-purpose admin gate for upgrade routes.
Edit boundaries: Centralised here so future PRs can add app-role plumbing
  without touching the routes.
Key entry points: `require_upgrade_admin`, `is_upgrade_admin`,
  `UPGRADE_ADMIN_ROLE`, `UPGRADE_ADMIN_OIDS_ENV`.
Risky contracts: `AUTH_DEV_BYPASS` bypasses the bearer check upstream but
  the synthetic identity still has to pass this gate; tests that exercise
  admin routes set `UPGRADE_ADMIN_OIDS` to include the dev-bypass oid.
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py`.
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException, status

from api.auth import DEV_BYPASS_OID, CallerIdentity, require_caller

UPGRADE_ADMIN_ROLE = "UpgradeAdmin"
UPGRADE_ADMIN_OIDS_ENV = "UPGRADE_ADMIN_OIDS"

def _allowed_oids() -> set[str]:
    raw = os.environ.get(UPGRADE_ADMIN_OIDS_ENV, "").strip()
    if not raw:
        return set()
    return {oid.strip() for oid in raw.split(",") if oid.strip()}


def is_upgrade_admin(caller: CallerIdentity) -> bool:
    """Return True when the caller is permitted to mutate upgrade state.

    Role matching is case-insensitive: a typo on the Azure portal
    (`upgradeadmin`, `UPGRADEADMIN`, …) is still recognised. The oid
    allowlist comparison stays case-sensitive because GUIDs are
    canonicalised by AAD.

    Audit P1 #11: in a deployed Container Apps revision (`CONTAINER_APP_NAME`
    set), the `DEV_BYPASS_OID` synthetic identity is explicitly rejected even
    if `UPGRADE_ADMIN_OIDS` accidentally contains it. The dev-bypass identity
    is meant for local debugging only; trusting it for the upgrade gate would
    let a stale `AUTH_DEV_BYPASS=true` env import escalate into a Container
    App image swap. Tests that exercise admin routes locally rely on the
    bypass identity but never set `CONTAINER_APP_NAME`.
    """
    if (
        caller.object_id == DEV_BYPASS_OID
        and os.environ.get("CONTAINER_APP_NAME")
    ):
        return False
    roles = caller.claims.get("roles") if isinstance(caller.claims, dict) else None
    if isinstance(roles, list):
        target = UPGRADE_ADMIN_ROLE.casefold()
        for role in roles:
            if isinstance(role, str) and role.casefold() == target:
                return True
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
                f"caller is not authorised for upgrade actions; requires the "
                f"'{UPGRADE_ADMIN_ROLE}' app role or the caller oid to be "
                f"listed in the {UPGRADE_ADMIN_OIDS_ENV} env."
            ),
        )
    return caller
