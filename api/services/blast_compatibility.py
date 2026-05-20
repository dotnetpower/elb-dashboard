"""Web BLAST compatibility contract for BLAST submit/pre-flight.

Responsibility: Web BLAST compatibility contract for BLAST submit/pre-flight
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `BlastCompatibilityContract`, `build_compatibility_contract`,
`_explicit_searchsp`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from api.services.sharding_precision import (
    PrecisionReport,
    normalize_sharding_mode,
    option_value,
)
from api.services.web_blast_searchsp import database_name_from_path, default_for_database

CompatibilityMode = Literal["precise", "calibration_required", "approximate"]


@dataclass(frozen=True)
class BlastCompatibilityContract:
    mode: CompatibilityMode
    level: str
    eligible: bool
    database: str
    search_space_source: str
    searchsp: int | None = None
    evidence: dict[str, Any] | None = None
    precision: dict[str, Any] | None = None
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "level": self.level,
            "eligible": self.eligible,
            "database": self.database,
            "search_space_source": self.search_space_source,
            "searchsp": self.searchsp,
            "evidence": self.evidence,
            "precision": self.precision,
            "blocking_errors": self.blocking_errors,
            "warnings": self.warnings,
        }


def build_compatibility_contract(
    *,
    database: str,
    options: dict[str, Any] | None,
    precision_report: PrecisionReport | None = None,
) -> BlastCompatibilityContract:
    """Return the Web BLAST compatibility contract for a normalized request."""
    opts = dict(options or {})
    db_name = database_name_from_path(database)
    verified_default = default_for_database(database)
    requested_mode = (
        precision_report.requested_mode if precision_report else normalize_sharding_mode(opts)
    )
    precision_dict = precision_report.as_dict() if precision_report else None
    warnings = list(precision_report.warnings if precision_report else [])
    blockers = list(precision_report.blocking_errors if precision_report else [])

    explicit_searchsp = _explicit_searchsp(opts)
    configured_searchsp = _positive_int(opts.get("db_effective_search_space")) or explicit_searchsp
    evidence = verified_default.as_dict() if verified_default is not None else None

    if precision_report is not None and not precision_report.eligible:
        return BlastCompatibilityContract(
            mode="approximate" if requested_mode == "approximate" else "calibration_required",
            level="blocked",
            eligible=False,
            database=db_name,
            search_space_source=_search_space_source(
                configured_searchsp=configured_searchsp,
                explicit_searchsp=explicit_searchsp,
                verified_value=verified_default.value if verified_default else None,
            ),
            searchsp=configured_searchsp,
            evidence=evidence,
            precision=precision_dict,
            blocking_errors=blockers,
            warnings=warnings,
        )

    if requested_mode == "approximate":
        return BlastCompatibilityContract(
            mode="approximate",
            level="approximate_sharded",
            eligible=True,
            database=db_name,
            search_space_source=_search_space_source(
                configured_searchsp=configured_searchsp,
                explicit_searchsp=explicit_searchsp,
                verified_value=verified_default.value if verified_default else None,
            ),
            searchsp=configured_searchsp,
            evidence=evidence,
            precision=precision_dict,
            blocking_errors=[],
            warnings=[
                "approximate sharding is explicitly allowed and may differ from "
                "full-DB Web BLAST-compatible output",
                *warnings,
            ],
        )

    if requested_mode == "precise":
        if verified_default is None:
            blockers.append(
                "precise Web BLAST-compatible sharding requires verified database "
                "search-space evidence"
            )
        elif configured_searchsp != verified_default.value and explicit_searchsp is not None:
            blockers.append(
                "effective search space does not match verified Web BLAST-compatible evidence"
            )

        if blockers:
            return BlastCompatibilityContract(
                mode="calibration_required",
                level="blocked",
                eligible=False,
                database=db_name,
                search_space_source=_search_space_source(
                    configured_searchsp=configured_searchsp,
                    explicit_searchsp=explicit_searchsp,
                    verified_value=verified_default.value if verified_default else None,
                ),
                searchsp=configured_searchsp,
                evidence=evidence,
                precision=precision_dict,
                blocking_errors=blockers,
                warnings=warnings,
            )

        assert verified_default is not None  # guarded by blockers check above
        if configured_searchsp is not None and configured_searchsp != verified_default.value:
            return BlastCompatibilityContract(
                mode="calibration_required",
                level="verified_database_nondefault_search_space",
                eligible=True,
                database=db_name,
                search_space_source=_search_space_source(
                    configured_searchsp=configured_searchsp,
                    explicit_searchsp=explicit_searchsp,
                    verified_value=verified_default.value,
                ),
                searchsp=configured_searchsp,
                evidence=evidence,
                precision=precision_dict,
                blocking_errors=[],
                warnings=[
                    "run is mechanically precise but does not match verified Web BLAST "
                    "search-space evidence",
                    *warnings,
                ],
            )

        return BlastCompatibilityContract(
            mode="precise",
            level="web_blast_compatible_sharded",
            eligible=True,
            database=db_name,
            search_space_source=_search_space_source(
                configured_searchsp=configured_searchsp,
                explicit_searchsp=explicit_searchsp,
                verified_value=verified_default.value,
            ),
            searchsp=configured_searchsp,
            evidence=evidence,
            precision=precision_dict,
            blocking_errors=[],
            warnings=warnings,
        )

    if verified_default is None:
        return BlastCompatibilityContract(
            mode="calibration_required",
            level="unverified_full_database",
            eligible=True,
            database=db_name,
            search_space_source=_search_space_source(
                configured_searchsp=configured_searchsp,
                explicit_searchsp=explicit_searchsp,
                verified_value=None,
            ),
            searchsp=configured_searchsp,
            evidence=None,
            precision=precision_dict,
            warnings=["database has no verified Web BLAST compatibility evidence", *warnings],
        )

    return BlastCompatibilityContract(
        mode="precise",
        level="verified_full_database_profile",
        eligible=True,
        database=db_name,
        search_space_source=_search_space_source(
            configured_searchsp=configured_searchsp,
            explicit_searchsp=explicit_searchsp,
            verified_value=verified_default.value,
        ),
        searchsp=configured_searchsp or verified_default.value,
        evidence=evidence,
        precision=precision_dict,
        warnings=warnings,
    )


def _explicit_searchsp(options: dict[str, Any]) -> int | None:
    additional = str(options.get("additional_options") or "")
    if additional:
        parsed = _positive_int(option_value(additional, "-searchsp"))
        if parsed is not None:
            return parsed
    return None


def _positive_int(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _search_space_source(
    *,
    configured_searchsp: int | None,
    explicit_searchsp: int | None,
    verified_value: int | None,
) -> str:
    if explicit_searchsp is not None:
        return "explicit_override"
    if (
        configured_searchsp is not None
        and verified_value is not None
        and configured_searchsp == verified_value
    ):
        return "verified_default"
    if configured_searchsp is not None:
        return "request"
    if verified_value is not None:
        return "verified_default_available"
    return "missing"
