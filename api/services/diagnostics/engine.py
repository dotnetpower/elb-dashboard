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
from api.services.diagnostics.rules import (
    evaluate_availability,
    evaluate_operational,
    evaluate_reliability,
    evaluate_security,
)
from api.services.diagnostics.snapshot import (
    DiagnosticTarget,
    gather_availability_snapshot,
    gather_operational_snapshot,
    gather_reliability_snapshot,
)
from api.services.monitor_cache import cached_snapshot
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

_Gatherer = Callable[[TokenCredential, DiagnosticTarget], dict[str, ResourceSnapshot]]
_Evaluator = Callable[[dict[str, ResourceSnapshot]], list[Finding]]

# Registry. Security reuses the reliability gatherer (it needs the same rich
# AKS / Storage / ACR detail snapshot).
_CATEGORY_GATHERERS: dict[str, _Gatherer] = {
    "reliability": gather_reliability_snapshot,
    "availability": gather_availability_snapshot,
    "security": gather_reliability_snapshot,
    "operational": gather_operational_snapshot,
}
_CATEGORY_EVALUATORS: dict[str, _Evaluator] = {
    "reliability": evaluate_reliability,
    "availability": evaluate_availability,
    "security": evaluate_security,
    "operational": evaluate_operational,
}

# The expensive part of a run is the ARM/K8s FETCH, not the pure rule
# evaluation. Categories that share a gatherer produce a byte-identical
# snapshot, so they share ONE cached fetch keyed by the snapshot "group" (not
# the category). This halves ARM load when an operator views Reliability and
# then Security back-to-back. The pure evaluation runs per request (microseconds
# over ~5 dicts) so each category still gets its own findings without a second
# fetch.
_SNAPSHOT_GROUP: dict[str, str] = {
    "reliability": "config",
    "security": "config",
    "availability": "runtime",
    "operational": "operational",
}


def supported_categories() -> list[str]:
    return sorted(_CATEGORY_GATHERERS)


def _gather_cached(
    category: str,
    credential: TokenCredential,
    target: DiagnosticTarget,
    *,
    fresh: bool,
) -> dict[str, ResourceSnapshot]:
    """Return the shared resource snapshot for ``category``, cached by group.

    The snapshot (the ARM/K8s IO) is memoised under a category-independent group
    key so categories sharing a gatherer (reliability + security) reuse one
    fetch. ``cached_snapshot`` serialises to JSON, so the ``ResourceSnapshot``
    objects are dumped on store and reconstructed on read; the injected ``cache``
    meta key is skipped.
    """
    gather = _CATEGORY_GATHERERS[category]
    group = _SNAPSHOT_GROUP.get(category, category)
    key = ":".join(
        [
            "diagnostics-snap",
            group,
            target.subscription_id,
            target.workload_resource_group,
            target.acr_resource_group,
            target.acr_name,
            target.storage_account_name,
        ]
    )

    def _loader() -> dict:
        snaps = gather(credential, target)
        return {kind: snap.model_dump() for kind, snap in snaps.items()}

    raw = cached_snapshot(key, _loader, ttl_seconds=30.0, force=fresh)
    return {
        kind: ResourceSnapshot(**payload)
        for kind, payload in raw.items()
        if kind != "cache" and isinstance(payload, dict)
    }


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


def _build_report(
    category: str,
    snapshots: dict[str, ResourceSnapshot],
) -> dict:
    evaluate = _CATEGORY_EVALUATORS[category]
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
    # Cache the expensive fetch (shared across categories with the same
    # gatherer); evaluate the rules per request (pure, microseconds).
    snapshots = _gather_cached(category, credential, target, fresh=fresh)
    return _build_report(category, snapshots)
