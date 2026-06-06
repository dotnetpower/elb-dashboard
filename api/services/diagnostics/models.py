"""Diagnostics finding + report models.

Read-only Reliability / Availability diagnostics data model shared by the
engine, the rule catalogs, and the HTTP route.

Responsibility: Define the Finding / DiagnosticReport / ResourceSnapshot shapes
    and the closed severity / category / resource-kind vocabularies the rule
    catalogs and the SPA agree on.
Edit boundaries: Pure data + light helpers only. No Azure SDK, no IO, no rule
    logic (that lives in `rules/`), no fetch (that lives in `snapshot.py`).
Key entry points: `Finding`, `DiagnosticReport`, `ResourceSnapshot`, `Severity`,
    `roll_up`.
Risky contracts: `Severity` / `DiagnosticCategory` string values are part of the
    SPA contract (`web/src/api/diagnostics.ts`); adding a value is additive but
    the SPA must default-handle unknown values. `indeterminate` is the only
    severity a permission-denied or fetch failure may produce — never `critical`.
Validation: `uv run pytest -q api/tests/test_diagnostics_rules.py
    api/tests/test_diagnostics_route.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["ok", "info", "warning", "critical", "indeterminate"]
DiagnosticCategory = Literal["reliability", "availability"]
ResourceKind = Literal["aks", "storage", "acr", "container_app", "api", "queue"]

# Severity ordering for sorting/rollup. Higher = more attention. `indeterminate`
# sits just under `critical`: "I could not check" is more actionable than a
# passing/info finding but must never outrank a confirmed `critical`.
_SEVERITY_ORDER: dict[str, int] = {
    "critical": 4,
    "indeterminate": 3,
    "warning": 2,
    "info": 1,
    "ok": 0,
}


def severity_rank(severity: str) -> int:
    """Sort key for a severity string (unknown values sort lowest)."""
    return _SEVERITY_ORDER.get(severity, -1)


class Finding(BaseModel):
    """A single best-practice check result for one configured resource.

    `observed` carries the sanitised raw signal (e.g. the AKS power state, the
    Storage SKU) so the SPA can render context without a second round-trip.
    """

    id: str = Field(description="Stable rule id, e.g. 'aks.provisioning_state'.")
    category: DiagnosticCategory
    pillar: str = Field(description="WAF pillar label, e.g. 'Reliability'.")
    resource_kind: ResourceKind
    resource_name: str = Field(default="", description="Short, sanitised resource name.")
    severity: Severity
    title: str
    detail: str
    recommendation: str = ""
    doc_url: str = ""
    rule_version: str = "1"
    # When True the finding describes a deliberate charter/cost decision, not a
    # defect; rules use this to cap severity at `info` instead of `warning`.
    expected_by_charter: bool = False
    observed: dict[str, str] = Field(default_factory=dict)


class ResourceSnapshot(BaseModel):
    """Result of fetching one resource kind for the engine.

    `available=False` with a `reason` means the rules that read this resource
    must emit `indeterminate` (never a fabricated `ok`/`critical`). `access`
    distinguishes a permission denial (expected for a Reader) from a transient
    fetch error, so the SPA can render a "verify with a higher role" banner.
    """

    kind: ResourceKind
    available: bool = True
    reason: str = ""
    access: Literal["ok", "denied", "error", "timeout"] = "ok"
    # Heterogeneous per-resource payload; opaque to the engine, read by rules.
    data: dict = Field(default_factory=dict)


class DiagnosticReport(BaseModel):
    """The full response for one category."""

    category: DiagnosticCategory
    generated_at: str
    findings: list[Finding] = Field(default_factory=list)
    rollup: dict[str, int] = Field(default_factory=dict)
    # True when at least one resource could not be verified, so the SPA can show
    # the "N items could not be verified with your role" banner.
    has_indeterminate: bool = False


def roll_up(findings: list[Finding]) -> dict[str, int]:
    """Count findings by severity for the page's rollup chips."""
    counts = {sev: 0 for sev in _SEVERITY_ORDER}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
