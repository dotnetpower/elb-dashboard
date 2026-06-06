"""Shared helpers for the diagnostic rule catalogs.

Responsibility: Build `indeterminate` findings from an unavailable snapshot and
    sanitise short resource names, so every rule module classifies a
    failure/permission-denial identically.
Edit boundaries: Pure helpers only. No fetch, no Azure SDK, no severity policy
    beyond the indeterminate mapping.
Key entry points: `indeterminate_for`, `short_name`.
Risky contracts: `indeterminate_for` MUST map `access="denied"` to a Reader-safe
    message and never produce `critical`.
Validation: `uv run pytest -q api/tests/test_diagnostics_rules.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.diagnostics.models import Finding, ResourceSnapshot
from api.services.sanitise import sanitise

_ACCESS_MESSAGE: dict[str, str] = {
    "denied": "Your role cannot read this resource, so it could not be verified.",
    "timeout": "The check timed out before the resource responded.",
    "error": "The resource could not be read due to a transient error.",
    "ok": "The resource could not be read.",
}

_ACCESS_RECOMMENDATION: dict[str, str] = {
    "denied": "Re-run with a role that can read this resource (e.g. Reader on the resource group).",
    "timeout": "Re-run the diagnostic; if it persists, check ARM throttling "
    "/ network reachability.",
    "error": "Re-run the diagnostic; if it persists, inspect the resource directly.",
    "ok": "Re-run the diagnostic.",
}


def short_name(value: Any) -> str:
    """Sanitised, length-capped resource name for display."""
    return sanitise(str(value or "")).strip()[:120]


def indeterminate_for(
    snap: ResourceSnapshot,
    *,
    category: str,
    pillar: str,
    resource_kind: str,
    id: str,
    title: str,
    doc_url: str = "",
) -> Finding:
    """Build the single `indeterminate` finding for an unavailable snapshot.

    A permission denial (`access="denied"`) is the expected Reader path — it is
    classified `indeterminate`, never `critical`, so the Persona Matrix stays
    green.
    """
    access = snap.access if snap.access in _ACCESS_MESSAGE else "error"
    detail = _ACCESS_MESSAGE[access]
    if snap.reason and access not in {"denied"}:
        detail = f"{detail} ({sanitise(snap.reason)[:80]})"
    return Finding(
        id=id,
        category=category,  # type: ignore[arg-type]
        pillar=pillar,
        resource_kind=resource_kind,  # type: ignore[arg-type]
        severity="indeterminate",
        title=title,
        detail=detail,
        recommendation=_ACCESS_RECOMMENDATION[access],
        doc_url=doc_url,
        observed={"access": access},
    )
