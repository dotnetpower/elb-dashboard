"""Opt-in RBAC enforcement gate for state-changing OpenAPI proxy calls.

Module summary: The ``/api/aks/openapi/proxy`` route auto-injects the admin
``X-ELB-API-Token`` and forwards browser "Try it" / curl calls to the
``elb-openapi`` pod. Because the dashboard validates tenant membership but —
with OBO flows forbidden by charter §12 — cannot enforce a per-caller Azure
RBAC gate, any authenticated tenant member (including a subscription Reader)
can drive state-changing calls through the admin token. This module adds an
**opt-in** gate that, when enabled, only lets callers who actually hold a
write role (Contributor / Owner / AKS write) on the target resource group
drive the mutating verbs (POST / PUT / PATCH / DELETE).

The gate ships **default-OFF** per charter §12a Rule 4: with
``ENFORCE_OPENAPI_EXEC_RBAC`` unset / ``false`` the legacy behaviour (any
tenant member can execute) is preserved exactly. Flipping it ``true`` is an
operator decision; the api managed identity then needs
``Microsoft.Authorization/roleAssignments/read`` at the subscription so it
can resolve the caller's effective roles (the ``Reader`` built-in already
grants this).

Responsibility: Pure decision logic — evaluate whether a caller may drive a
    given OpenAPI proxy method, reusing ``compute_caller_permissions`` for
    the RBAC lookup. Never raises HTTP; the route translates the decision.
Edit boundaries: No FastAPI / HTTP imports here. RBAC enumeration stays in
    ``api.services.me_permissions``; this module only re-interprets its
    result as a security gate (fail-CLOSED when enforced and the lookup is
    indeterminate, vs the UX surface's fail-OPEN).
Key entry points: ``evaluate_openapi_exec_gate``, ``ExecGateDecision``,
    ``is_exec_rbac_enforced``.
Risky contracts: ``compute_caller_permissions`` degrades OPEN (all caps
    ``True``, ``degraded=True``) on enumeration failure because it is a UX
    affordance. This gate MUST treat ``degraded=True`` as DENY when
    enforcement is on — otherwise an MI that cannot read role assignments
    would silently disable the gate. Read-only methods are never gated.
Validation: ``uv run pytest -q api/tests/test_openapi_exec_gate.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from azure.core.credentials import TokenCredential

from api.auth import CallerIdentity, is_dev_bypass_caller
from api.services.me_permissions import compute_caller_permissions

LOGGER = logging.getLogger(__name__)

_ENFORCE_ENV = "ENFORCE_OPENAPI_EXEC_RBAC"

# Verbs that mutate state on the elb-openapi service. GET / HEAD / OPTIONS
# are read-only and never gated.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_exec_rbac_enforced() -> bool:
    """Return True when ``ENFORCE_OPENAPI_EXEC_RBAC`` opts into the gate.

    Default OFF (unset / any value other than a truthy token) preserves the
    legacy "any tenant member can execute" behaviour per charter §12a
    Rule 4.
    """
    return os.environ.get(_ENFORCE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ExecGateDecision:
    """Outcome of the OpenAPI proxy execution gate.

    ``allowed`` — whether the route should forward the call.
    ``reason``  — short machine token for logs / tests.
    ``status_code`` — HTTP status the route should raise when denied
        (``0`` when allowed).
    ``detail`` — error body the route should return when denied
        (empty when allowed). Never contains a token.
    """

    allowed: bool
    reason: str
    status_code: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def _allow(reason: str) -> ExecGateDecision:
    return ExecGateDecision(allowed=True, reason=reason)


def evaluate_openapi_exec_gate(
    credential: TokenCredential,
    *,
    caller: CallerIdentity,
    method: str,
    subscription_id: str,
    resource_group: str,
    cluster_name: str | None = None,
) -> ExecGateDecision:
    """Decide whether ``caller`` may drive an OpenAPI proxy ``method``.

    Decision order (first match wins):

    1. Enforcement disabled (``ENFORCE_OPENAPI_EXEC_RBAC`` off) → allow.
    2. Read-only method (GET / HEAD / OPTIONS) → allow.
    3. Dev-bypass caller (local debug) → allow.
    4. RBAC lookup indeterminate (``degraded``) → DENY (fail-closed).
    5. Caller holds a write role at the RG scope → allow.
    6. Otherwise → DENY with the caller's matched roles for the tooltip.

    The RBAC lookup is delegated to ``compute_caller_permissions`` (cached
    60 s per ``(oid, scope)``), so the gate adds at most one ARM call per
    caller per scope per minute.
    """
    if not is_exec_rbac_enforced():
        return _allow("enforcement_disabled")

    if method.upper() not in _STATE_CHANGING_METHODS:
        return _allow("read_only_method")

    # Local debug identity — never has a real Azure principal to evaluate.
    if is_dev_bypass_caller(caller):
        return _allow("dev_bypass")

    perms = compute_caller_permissions(
        credential,
        caller_oid=caller.object_id or "",
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )

    # Fail CLOSED when the lookup could not be resolved. The UX surface
    # degrades open (so a transient ARM hiccup never greys out a button),
    # but a security gate that cannot prove the caller is authorised MUST
    # deny — otherwise an MI lacking roleAssignments/read silently
    # disables the gate.
    if perms.degraded:
        LOGGER.warning(
            "openapi exec gate: RBAC lookup indeterminate, denying "
            "(method=%s rg=%s reason=%s)",
            method.upper(),
            resource_group,
            perms.reason,
        )
        return ExecGateDecision(
            allowed=False,
            reason="rbac_indeterminate",
            status_code=403,
            detail={
                "code": "openapi_exec_rbac_indeterminate",
                "message": (
                    "Execution is gated by RBAC but the caller's role "
                    "assignments could not be resolved. The api managed "
                    "identity needs Microsoft.Authorization/roleAssignments/"
                    "read at the subscription scope."
                ),
            },
        )

    if perms.can_write:
        return _allow("rbac_write_ok")

    return ExecGateDecision(
        allowed=False,
        reason="rbac_forbidden",
        status_code=403,
        detail={
            "code": "openapi_exec_forbidden",
            "message": (
                "You do not have write access to this resource group. "
                "Driving state-changing OpenAPI calls requires a "
                "Contributor / Owner (or AKS write) role on "
                f"{resource_group}."
            ),
            "matched_roles": list(perms.matched_role_names),
        },
    )
