"""Settings → shared M2M token disclosure route.

Responsibility: Expose the `elb-openapi` shared admin token
(``ELB_OPENAPI_API_TOKEN``) to authenticated dashboard callers so the SPA's
"Copy curl" surface can inline the actual value instead of the
``$ELB_API_TOKEN`` placeholder. The backend already accepts this token as an
alternative to the MSAL bearer via the universal M2M path in ``api/auth.py``;
this route only exposes the value the api sidecar already knows about, it
never mints new tokens.
Edit boundaries: HTTP shaping + auth only. Token resolution reuses
``api.auth._resolve_expected_openapi_token`` so the read is coherent with
the auth-time check — both look at the same deploy-time env / Redis cache.
Key entry points: ``get_openapi_token``.
Risky contracts: The returned ``token`` is a shared admin credential with no
Azure RBAC gate. This surface is deliberately permissive — any
``require_caller``-authenticated caller can read it — per the operator
policy that ships with the universal M2M path (dashboard log-in is the
trust boundary). If a future deployment needs Reader → shared-token
escalation blocked, replace ``require_caller`` with a Contributor role
check here.
Validation: ``uv run pytest -q api/tests/test_settings_openapi_token.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from api.auth import CallerIdentity, _resolve_expected_openapi_token, require_caller

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
def get_openapi_token(
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the shared M2M token the api sidecar accepts.

    Response shape:
      * ``token``: the shared admin token, or ``""`` when the deployment has
        none configured (gate on but no ``ELB_OPENAPI_API_TOKEN`` / cache
        entry). The SPA falls back to a ``$ELB_API_TOKEN`` placeholder in
        that case.
      * ``gate_enabled``: whether ``ALLOW_OPENAPI_TOKEN_AUTH`` is on. When
        false the token would not authenticate anything even if non-empty,
        so the SPA can render an inline hint.
    """
    import os

    token = _resolve_expected_openapi_token()
    gate_enabled = os.environ.get("ALLOW_OPENAPI_TOKEN_AUTH", "").lower() == "true"
    return {"token": token, "gate_enabled": gate_enabled}
