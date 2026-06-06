"""Diagnostics run engine.

Orchestrates one read-only diagnostic run: gather the shared resource snapshot
(bounded + isolated), evaluate the category's rules, sanitise, and assemble a
`DiagnosticReport`. Memoised via the monitor snapshot cache so a double-click /
multi-tab / repeat entry does not re-hammer ARM.

Responsibility: Glue `snapshot.py` (fetch) to `rules/` (evaluate) and shape a
    cached, sanitised `DiagnosticReport`. No best-practice logic, no Azure SDK
    calls of its own.
Edit boundaries: Caching, rollup, sanitisation pass, structured run log. Add a
    new category by extending `_CATEGORY_EVALUATORS` + `_CATEGORY_GATHERERS`.
Key entry points: `run_diagnostic`.
Risky contracts: Read-only — no side effects, so no idempotency/concurrency
    hazard. Every finding's text fields are sanitised before return. A category
    with no gatherer raises `ValueError` (the route maps it to 404).
Validation: `uv run pytest -q api/tests/test_diagnostics_route.py
    api/tests/test_diagnostics_rules.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from azure.core.credentials import TokenCredential

from api.services.diagnostics.models import (
    DiagnosticReport,
    Finding,
    ResourceSnapshot,
    roll_up,
    severity_rank,
)
from api.services.diagnostics.rules import evaluate_availability, evaluate_reliability
from api.services.diagnostics.snapshot import (
    DiagnosticTarget,
    gather_availability_snapshot,
    gather_reliability_snapshot,
)
from api.services.monitor_cache import cached_snapshot
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_Gatherer = Callable[[TokenCredential, DiagnosticTarget], dict[str, ResourceSnapshot]]
_Evaluator = Callable[[dict[str, ResourceSnapshot]], list[Finding]]

# Registry. Phase 3 adds "availability".
_CATEGORY_GATHERERS: dict[str, _Gatherer] = {
    "reliability": gather_reliability_snapshot,
    "availability": gather_availability_snapshot,
}
_CATEGORY_EVALUATORS: dict[str, _Evaluator] = {
    "reliability": evaluate_reliability,
    "availability": evaluate_availability,
}


def supported_categories() -> list[str]:
    return sorted(_CATEGORY_GATHERERS)


def _sanitise_finding(finding: Finding) -> Finding:
    """Defence-in-depth: re-sanitise every user-facing text field before return.

    Rule authors should already pass clean text, but this guarantees no SAS /
    token / GUID leaks through even a sloppy rule or an echoed Azure message.
    """
    return finding.model_copy(
        update={
            "resource_name": sanitise(finding.resource_name)[:120],
            "title": sanitise(finding.title)[:200],
            "detail": sanitise(finding.detail)[:500],
            "recommendation": sanitise(finding.recommendation)[:300],
            "observed": {k: sanitise(str(v))[:200] for k, v in finding.observed.items()},
        }
    )


def _build_report(category: str, credential: TokenCredential, target: DiagnosticTarget) -> dict:
    gather = _CATEGORY_GATHERERS[category]
    evaluate = _CATEGORY_EVALUATORS[category]
    snapshots = gather(credential, target)
    findings = [_sanitise_finding(f) for f in evaluate(snapshots)]
    # Most-actionable first: severity desc, then resource kind, then id.
    findings.sort(key=lambda f: (-severity_rank(f.severity), f.resource_kind, f.id))
    rollup = roll_up(findings)
    report = DiagnosticReport(
        category=category,  # type: ignore[arg-type]
        generated_at=datetime.now(UTC).isoformat(),
        findings=findings,
        rollup=rollup,
        has_indeterminate=rollup.get("indeterminate", 0) > 0,
    )
    LOGGER.info(
        "diagnostics run category=%s rules=%d ok=%d info=%d "
        "warning=%d critical=%d indeterminate=%d",
        category,
        len(findings),
        rollup.get("ok", 0),
        rollup.get("info", 0),
        rollup.get("warning", 0),
        rollup.get("critical", 0),
        rollup.get("indeterminate", 0),
    )
    return report.model_dump()


def run_diagnostic(
    category: str,
    credential: TokenCredential,
    target: DiagnosticTarget,
    *,
    fresh: bool = False,
) -> dict:
    """Run (or serve a cached) diagnostic report for one category.

    Raises `ValueError` for an unknown category (the route maps it to 404).
    Read-only; safe to call concurrently.
    """
    if category not in _CATEGORY_GATHERERS:
        raise ValueError(f"unknown diagnostic category: {category}")
    key = ":".join(
        [
            "diagnostics",
            category,
            target.subscription_id,
            target.workload_resource_group,
            target.acr_resource_group,
            target.acr_name,
            target.storage_account_name,
        ]
    )
    return cached_snapshot(
        key,
        lambda: _build_report(category, credential, target),
        ttl_seconds=30.0,
        force=fresh,
    )
