"""Declarative rule-spec framework for the diagnostics catalogs.

Most Well-Architected / Cloud-Adoption-Framework checks are a single
boolean/threshold predicate over one fetched configuration field. Expressing
each as a `RuleSpec` (instead of a bespoke function) keeps the ~100-entry
catalog compact, uniformly sanitised, and trivially golden-testable.

Responsibility: Define `RuleSpec`, the compliance predicate helpers
    (`want_true` / `want_false` / `equals_ci` / …), and the generic
    `evaluate_specs` that turns a spec list + one resource dict into Findings.
Edit boundaries: Pure data + pure evaluation. No Azure SDK, no IO, no fetch.
Key entry points: `RuleSpec`, `evaluate_specs`, the `want_*` predicates.
Risky contracts: A predicate returning `None` means "field not available /
    unknown" → the spec is SKIPPED (never a fabricated `ok` or `bad`). A `False`
    verdict emits the spec's `bad_severity`, which is never `indeterminate`
    (that severity is reserved for unavailable snapshots).
Validation: `uv run pytest -q api/tests/test_diagnostics_specs.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from api.services.diagnostics.models import Finding
from api.services.diagnostics.rules.common import short_name

LOGGER = logging.getLogger(__name__)

# A compliance predicate: given the field value, return True (compliant / ok),
# False (not compliant / emit bad_severity), or None (unknown → skip the spec).
Predicate = Callable[[Any], bool | None]


@dataclass(frozen=True)
class RuleSpec:
    """One declarative best-practice check over a single resource field."""

    id: str
    resource_kind: str
    pillar: str
    field: str
    title_ok: str
    title_bad: str
    detail_ok: str
    detail_bad: str
    recommendation: str
    doc_url: str
    bad_severity: str = "warning"
    compliant: Predicate = bool
    expected_by_charter: bool = False
    # Optional override: derive the verdict from the WHOLE resource dict instead
    # of a single field (for two-field checks). When set, `field`/`compliant`
    # are ignored for the verdict but `field` still names the `observed` key.
    compliant_resource: Callable[[dict[str, Any]], bool | None] | None = field(default=None)


# --------------------------------------------------------------------------- predicates


def want_true(value: Any) -> bool | None:
    """Compliant when the field is exactly True; unknown (None) → skip."""
    return None if value is None else value is True


def want_false(value: Any) -> bool | None:
    """Compliant when the field is exactly False; unknown (None) → skip."""
    return None if value is None else value is False


def equals_ci(target: str) -> Predicate:
    """Compliant when the field equals `target`, case-insensitively."""

    def _check(value: Any) -> bool | None:
        if value is None:
            return None
        return str(value).strip().lower() == target.lower()

    return _check


def in_ci(*options: str) -> Predicate:
    """Compliant when the field is one of `options`, case-insensitively."""
    lowered = {o.lower() for o in options}

    def _check(value: Any) -> bool | None:
        if value is None:
            return None
        return str(value).strip().lower() in lowered

    return _check


def set_and_not(*bad_values: str) -> Predicate:
    """Compliant when the field is set and NOT one of `bad_values`.

    Used for "a value is configured" checks (e.g. network policy set, upgrade
    channel set) where `None`/empty/"none" means not configured (bad), but the
    field being genuinely absent from the API is also `None` → we treat empty
    string / "none" as bad and a true `None` as skip is impossible to
    distinguish, so empty/"none" → bad, real None → bad too (the field is
    expected on current API versions). Callers that need skip-on-None use
    `want_true` instead.
    """
    bad = {b.lower() for b in bad_values}

    def _check(value: Any) -> bool | None:
        if value is None:
            return False
        text = str(value).strip().lower()
        if text == "" or text in bad:
            return False
        return True

    return _check


# --------------------------------------------------------------------------- evaluator


def evaluate_specs(
    specs: list[RuleSpec],
    resource: dict[str, Any],
    *,
    category: str,
    resource_name: str,
) -> list[Finding]:
    """Evaluate a spec list against one resource dict.

    Skips a spec whose predicate returns `None` (field unavailable) so the
    catalog never fabricates a result for data it could not read.
    """
    out: list[Finding] = []
    for spec in specs:
        value = resource.get(spec.field)
        try:
            verdict = (
                spec.compliant_resource(resource)
                if spec.compliant_resource is not None
                else spec.compliant(value)
            )
        except Exception as exc:
            # A malformed/unexpected value type must not abort the whole
            # catalog. Log at debug, treat as "unknown", and skip — never
            # fabricate a verdict from a value the predicate could not handle.
            LOGGER.debug("diagnostics spec %s predicate failed: %s", spec.id, type(exc).__name__)
            continue
        if verdict is None:
            continue
        ok = bool(verdict)
        out.append(
            Finding(
                id=spec.id,
                category=category,  # type: ignore[arg-type]
                pillar=spec.pillar,
                resource_kind=spec.resource_kind,  # type: ignore[arg-type]
                resource_name=resource_name,
                severity="ok" if ok else spec.bad_severity,  # type: ignore[arg-type]
                title=spec.title_ok if ok else spec.title_bad,
                detail=spec.detail_ok if ok else spec.detail_bad,
                recommendation="" if ok else spec.recommendation,
                doc_url=spec.doc_url,
                rule_version="waf-2026-06",
                expected_by_charter=spec.expected_by_charter,
                observed={spec.field: short_name(value)},
            )
        )
    return out
