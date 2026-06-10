"""Optional entry gate: require a read RBAC role on the dashboard scope.

Module summary: `require_caller` validates tenant membership only — any
authenticated member of the configured Entra tenant can reach the dashboard,
even with zero Azure RBAC on the deployment's resource group / subscription
(Broken Access Control, OWASP A01 — see docs/copilot/security-audit-followup.md
#1). This module adds an **opt-in** entry gate that, when enabled, only lets
callers who actually hold a read role (Reader / Contributor / Owner / the AKS
read roles / Storage Blob Data reader) on the platform scope load the
dashboard. It is wired onto the SPA's identity bootstrap (`GET /api/me`); a
denied caller gets a clean 403 that the SPA renders as an "access denied"
screen instead of a half-broken dashboard.

The gate ships **default-OFF** per charter §12a Rule 4: with
`ENFORCE_DASHBOARD_RBAC` unset / `false` the legacy behaviour (any tenant
member loads the dashboard) is preserved exactly. Flipping it `true` requires
the shared managed identity to hold `Microsoft.Authorization/roleAssignments/read`
at subscription scope (the built-in `Reader` grants this) so it can resolve the
caller's effective roles — otherwise enumeration fails and the gate degrades
OPEN (see below) to avoid locking everyone out.

Responsibility: Single-purpose entry gate — decide whether a validated caller
    may load the dashboard, reusing `compute_caller_permissions` for the RBAC
    lookup. Read-only; never grants, never modifies.
Edit boundaries: The RBAC enumeration stays in `api.services.me_permissions`;
    this module only re-interprets `can_read` as an entry decision and raises
    the HTTP 403. Per-route authorization (the full §1 design) is intentionally
    out of scope — this is the bootstrap/UX entry gate, not a per-action
    boundary. ARM still enforces real authorization at submit time.
Key entry points: `require_dashboard_access`, `has_dashboard_read_access`,
    `is_dashboard_rbac_enforced`, `ENFORCE_DASHBOARD_RBAC_ENV`,
    `DASHBOARD_ACCESS_DENIED_CODE`.
Risky contracts: Unlike the upgrade-admin gate (which degrades CLOSED), this
    entry gate degrades OPEN when enumeration fails (`degraded=True`) or the
    platform scope is unconfigured — a transient ARM hiccup or a missing
    `AZURE_SUBSCRIPTION_ID` must never lock out a legitimate operator. The
    degraded condition is all-or-nothing (the MI either can read role
    assignments or cannot); it is never selectively true for the attacker, so
    fail-open here cannot be abused to slip a no-role caller past the gate.
Validation: `uv run pytest -q api/tests/test_dashboard_access.py
    api/tests/test_persona_matrix.py`.
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException, status

from api.auth import CallerIdentity, is_dev_bypass_caller, require_caller

LOGGER = logging.getLogger(__name__)

ENFORCE_DASHBOARD_RBAC_ENV = "ENFORCE_DASHBOARD_RBAC"
DASHBOARD_ACCESS_DENIED_CODE = "dashboard_access_denied"


def is_dashboard_rbac_enforced() -> bool:
    """Return True when `ENFORCE_DASHBOARD_RBAC=true` is set.

    Read at call time so tests can flip the env via `monkeypatch.setenv`
    without re-importing the module. Default OFF preserves the legacy
    "any tenant member loads the dashboard" behaviour (charter §12a Rule 4).
    """
    return os.environ.get(ENFORCE_DASHBOARD_RBAC_ENV, "").strip().lower() == "true"


def has_dashboard_read_access(caller: CallerIdentity) -> bool:
    """Return True when ``caller`` may load the dashboard.

    Resolution:
      * dev-bypass identity → True (local debug; never honoured in a deployed
        Container App, see `is_dev_bypass_caller`).
      * no caller oid → False.
      * platform scope unconfigured (`AZURE_SUBSCRIPTION_ID` unset) → True
        (cannot evaluate; fail OPEN so a mis-set unit/dev env never locks out).
      * else `compute_caller_permissions` at the platform scope:
          - enumeration failed (`degraded=True`) → True (fail OPEN; the MI
            likely lacks `roleAssignments/read` — better a visible warning than
            a tenant-wide lockout). ARM still enforces real authorization.
          - `can_read` → True.
          - otherwise → False (authenticated tenant member with no role).
    """
    if is_dev_bypass_caller(caller):
        return True
    if not caller or not caller.object_id:
        return False
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not subscription_id:
        # No platform scope configured — cannot evaluate. Fail OPEN.
        return True
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
            "dashboard_access: permission check failed: %s", type(exc).__name__
        )
        return True  # fail OPEN — never lock out on an unexpected error
    if perms.degraded:
        # Enumeration failed (e.g. MI lacks roleAssignments/read at the
        # subscription). All-or-nothing, never selectively true for one
        # caller, so opening here cannot let a no-role caller slip past.
        LOGGER.warning(
            "dashboard_access: RBAC enumeration degraded, allowing entry "
            "(reason=%s) — grant the dashboard managed identity Reader at "
            "subscription scope to enforce the gate",
            perms.reason,
        )
        return True
    return bool(perms.can_read)


def require_dashboard_access(
    caller: CallerIdentity = Depends(require_caller),
) -> CallerIdentity:
    """FastAPI dependency: 403 when the entry gate is enforced and the caller
    holds no read RBAC role on the dashboard scope.

    Default-OFF: when `ENFORCE_DASHBOARD_RBAC` is unset / false this is a
    transparent pass-through equivalent to `require_caller`.
    """
    if not is_dashboard_rbac_enforced():
        return caller
    if has_dashboard_read_access(caller):
        return caller

    from api.services.sanitise import redact_oid

    resource_group = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    LOGGER.info(
        "dashboard_access denied for oid=%s rg=%s",
        redact_oid(caller.object_id),
        resource_group or "(unset)",
    )
    scope_label = (
        f"resource group '{resource_group}'" if resource_group else "the subscription"
    )
    sub_suffix = f" (subscription {subscription_id})" if subscription_id else ""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": DASHBOARD_ACCESS_DENIED_CODE,
            "message": (
                "You are signed in but have no Azure role on the dashboard's "
                f"{scope_label}{sub_suffix}. Ask a subscription owner or "
                "administrator to grant you at least the Reader role there (or "
                "on the subscription), then retry."
            ),
            "resource_group": resource_group,
            "subscription_id": subscription_id,
        },
    )
