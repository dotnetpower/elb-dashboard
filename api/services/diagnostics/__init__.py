"""Diagnostics service package.

Read-only Reliability / Availability best-practice checks over the configured
Azure resources. See `docs/architecture/diagnostics.md` for the design of
record.
"""

from __future__ import annotations

from api.services.diagnostics.models import (
    DiagnosticCategory,
    DiagnosticReport,
    Finding,
    ResourceSnapshot,
    Severity,
)

__all__ = [
    "DiagnosticCategory",
    "DiagnosticReport",
    "Finding",
    "ResourceSnapshot",
    "Severity",
]
