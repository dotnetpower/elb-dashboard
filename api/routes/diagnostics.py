"""Diagnose & solve problems — Reliability / Availability routes.

Read-only best-practice checks over the configured Azure resources. Unlike the
`/api/monitor/*` surface (which degrades open to an empty payload), this surface
reports a fetch failure / permission denial as an `indeterminate` finding, so a
"could not check" never masquerades as "no problems found".

Responsibility: HTTP validation + auth + response shaping for
    `GET /api/diagnostics/{category}`. Delegate all fetch + rule logic to
    `api.services.diagnostics`.
Edit boundaries: Keep HTTP concerns here; no Azure SDK, no rule logic.
Key entry points: `diagnostic_report`.
Risky contracts: Every route enforces `require_caller`. This is a plain
    request/response GET (no SSE), so §12a Rule 5 does not apply. Findings are
    already sanitised by the engine.
Validation: `uv run pytest -q api/tests/test_diagnostics_route.py
    api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.routes.monitor.common import _sub_default
from api.services.diagnostics.engine import run_diagnostic, supported_categories
from api.services.diagnostics.snapshot import DiagnosticTarget

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("/{category}")
def diagnostic_report(
    category: str,
    subscription_id: str = Query(default=""),
    workload_resource_group: str = Query(default=""),
    acr_resource_group: str = Query(default=""),
    acr_name: str = Query(default=""),
    storage_account_name: str = Query(default=""),
    region: str = Query(default=""),
    fresh: bool = Query(default=False),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run a read-only Reliability / Availability diagnostic for the configured
    resources and return severity-ranked findings.

    `fresh=true` bypasses the 30 s result cache (used by the page's "Re-run").
    An unknown category is 404; a missing subscription is 400 (the SPA renders
    "open the Setup Wizard first" before calling).
    """
    if category not in supported_categories():
        raise HTTPException(404, f"unknown diagnostic category: {category}")
    sub = subscription_id or _sub_default()
    if not sub:
        raise HTTPException(400, "subscription_id required")

    from api.routes import monitor as monitor_package

    target = DiagnosticTarget(
        subscription_id=sub,
        workload_resource_group=workload_resource_group,
        acr_resource_group=acr_resource_group,
        acr_name=acr_name,
        storage_account_name=storage_account_name,
        region=region,
    )
    del caller  # auth enforced by dependency; identity not needed here.
    return run_diagnostic(category, monitor_package.get_credential(), target, fresh=fresh)
