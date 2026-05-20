"""Precision policy for sharded BLAST submissions.

Responsibility: Precision policy for sharded BLAST submissions
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `PrecisionReport`, `normalize_sharding_mode`, `option_value`,
`outfmt_is_merge_compatible`, `merge_format_for_outfmt`, `positive_int`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

ShardingMode = Literal["off", "approximate", "precise"]
PrecisionLevel = Literal[
    "full",
    "precise_single_query",
    "precise_tabular",
    "precise_tabular_split",
    "precise_xml",
    "precise_xml_split",
    "approximate",
    "blocked",
]


@dataclass(frozen=True)
class PrecisionReport:
    requested_mode: ShardingMode
    effective_mode: ShardingMode
    precision_level: PrecisionLevel
    eligible: bool
    merge_strategy: str
    required_options: dict[str, Any] = field(default_factory=dict)
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "precision_level": self.precision_level,
            "eligible": self.eligible,
            "merge_strategy": self.merge_strategy,
            "required_options": self.required_options,
            "blocking_errors": self.blocking_errors,
            "warnings": self.warnings,
        }


def normalize_sharding_mode(options: Mapping[str, Any] | None) -> ShardingMode:
    """Return the explicit sharding mode, mapping legacy flags conservatively."""
    opts = options or {}
    raw = str(opts.get("sharding_mode") or "").strip().lower()
    if raw:
        if raw not in {"off", "approximate", "precise"}:
            raise ValueError("sharding_mode must be one of: off, approximate, precise")
        if raw == "off" and (
            positive_int(opts.get("db_partitions")) or opts.get("db_partition_prefix")
        ):
            raise ValueError("sharding_mode=off conflicts with db_partitions/db_partition_prefix")
        return raw  # type: ignore[return-value]
    if (
        opts.get("allow_approximate_sharding")
        or opts.get("db_auto_partition")
        or positive_int(opts.get("db_partitions"))
        or opts.get("db_partition_prefix")
    ):
        return "approximate"
    return "off"


def option_value(options: str, option: str) -> str | None:
    try:
        tokens = shlex.split(options or "")
    except ValueError as exc:
        raise ValueError(f"invalid blast options: {exc}") from exc
    prefix = f"{option}="
    for idx, token in enumerate(tokens):
        if token == option:
            return tokens[idx + 1] if idx + 1 < len(tokens) else None
        if token.startswith(prefix):
            return token.split("=", 1)[1]
    return None


def outfmt_is_merge_compatible(value: object | None) -> bool:
    """Return True for output layouts the shard merge engine supports."""
    return merge_format_for_outfmt(value) is not None


def merge_format_for_outfmt(value: object | None) -> Literal["tabular", "xml"] | None:
    """Return the supported shard merge family for a BLAST outfmt value."""
    if value in (None, ""):
        return "tabular"
    parts = str(value).strip().strip("'\"").split()
    if not parts:
        return "tabular"
    if parts[0] == "5" and len(parts) == 1:
        return "xml"
    if parts[0] == "6" and (len(parts) == 1 or parts[1] == "std"):
        return "tabular"
    return None


def positive_int(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def query_effective_search_spaces_error(value: object | None) -> str | None:
    """Return a validation error for query-level effective search-space input."""
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return "query_effective_search_spaces must be a list ordered by FASTA query order"
    if not isinstance(value, (list, tuple)):
        return "query_effective_search_spaces must be a list of positive integers"
    for item in value:
        if positive_int(item) is None:
            return "query_effective_search_spaces values must be positive integers"
    return None


def query_effective_search_spaces(value: object | None) -> list[int]:
    """Normalise query-level effective search spaces from ordered list input."""
    if query_effective_search_spaces_error(value) is not None:
        return []
    if value in (None, "") or not isinstance(value, (list, tuple)):
        return []
    spaces: list[int] = []
    for item in value:
        parsed = positive_int(item)
        if parsed is not None:
            spaces.append(parsed)
    return spaces


def uniform_query_effective_search_space(
    options: dict[str, Any], query_count: int | None
) -> int | None:
    """Return a shared query-level search space when every query has the same value."""
    spaces = query_effective_search_spaces(options.get("query_effective_search_spaces"))
    if not spaces:
        return None
    if query_count is not None and len(spaces) != query_count:
        return None
    unique = set(spaces)
    if len(unique) != 1:
        return None
    return spaces[0]


def build_precision_report(
    options: dict[str, Any] | None,
    *,
    query_count: int | None = None,
    db_stats_available: bool = False,
    shard_sets: list[int] | None = None,
) -> PrecisionReport:
    """Assess sharding precision for a submit/pre-flight request."""
    opts = dict(options or {})
    mode = normalize_sharding_mode(opts)
    warnings: list[str] = []
    blockers: list[str] = []
    required: dict[str, Any] = {}

    if query_count is not None:
        query_count = positive_int(query_count)

    if mode == "off":
        return PrecisionReport(
            requested_mode=mode,
            effective_mode="off",
            precision_level="full",
            eligible=True,
            merge_strategy="none",
        )

    additional = str(opts.get("additional_options") or "")
    additional_outfmt = option_value(additional, "-outfmt") if additional else None
    additional_searchsp = (
        positive_int(option_value(additional, "-searchsp")) if additional else None
    )
    outfmt = additional_outfmt if additional_outfmt is not None else opts.get("outfmt")
    merge_format = merge_format_for_outfmt(outfmt)
    if merge_format is None:
        blockers.append(
            "sharded result merge currently supports only outfmt 5, outfmt 6, or outfmt '6 std...'"
        )

    if not db_stats_available and not positive_int(opts.get("db_total_letters")):
        warnings.append(
            "full DB statistics are missing; shard-local statistics may change e-values"
        )

    if shard_sets is not None and len(shard_sets) == 0:
        blockers.append("no prepared shard layout is available for the selected database")

    if mode == "approximate":
        return PrecisionReport(
            requested_mode=mode,
            effective_mode="approximate",
            precision_level="blocked" if blockers else "approximate",
            eligible=not blockers,
            merge_strategy=(
                "xml_top_n"
                if not blockers and merge_format == "xml"
                else "tabular_top_n"
                if not blockers
                else "blocked"
            ),
            blocking_errors=blockers,
            warnings=[
                "approximate sharding can differ from full-DB BLAST because search "
                "statistics and tie order may differ",
                *warnings,
            ],
        )

    # precise mode: a single BLAST invocation can use only one -searchsp. Multi-query
    # precise is therefore allowed only when every query has the same supplied
    # effective search space; mixed spaces require future query-group splitting.
    configured_db_search_space = positive_int(opts.get("db_effective_search_space"))
    if (
        configured_db_search_space is not None
        and additional_searchsp is not None
        and configured_db_search_space != additional_searchsp
    ):
        blockers.append("db_effective_search_space conflicts with additional_options -searchsp")
    supplied_db_search_space = configured_db_search_space or additional_searchsp
    query_spaces_error = query_effective_search_spaces_error(
        opts.get("query_effective_search_spaces")
    )
    uniform_query_search_space = uniform_query_effective_search_space(opts, query_count)
    query_search_spaces = query_effective_search_spaces(opts.get("query_effective_search_spaces"))
    split_query_search_spaces = False
    if query_spaces_error is not None:
        blockers.append(query_spaces_error)
    if (
        supplied_db_search_space is not None
        and uniform_query_search_space is not None
        and supplied_db_search_space != uniform_query_search_space
    ):
        blockers.append("db_effective_search_space conflicts with query_effective_search_spaces")
    if (
        supplied_db_search_space is not None
        and query_search_spaces
        and uniform_query_search_space is None
    ):
        blockers.append(
            "db_effective_search_space is single-query only and conflicts with "
            "mixed query_effective_search_spaces"
        )
    effective_search_space = supplied_db_search_space or uniform_query_search_space
    if query_count is None:
        blockers.append("precise sharding requires query metadata")
    elif query_count > 1:
        if query_spaces_error is not None:
            required["query_effective_search_spaces"] = (
                "ordered list with one positive integer per query"
            )
        elif not query_search_spaces:
            if supplied_db_search_space is not None:
                blockers.append(
                    "db_effective_search_space is single-query only; precise multi-query "
                    "sharding requires query_effective_search_spaces"
                )
            else:
                blockers.append(
                    "precise multi-query sharding requires query_effective_search_spaces"
                )
            required["query_effective_search_spaces"] = (
                "ordered list with one positive integer per query"
            )
        elif len(query_search_spaces) != query_count:
            blockers.append("query_effective_search_spaces count must match query_count")
        elif uniform_query_search_space is None:
            split_query_search_spaces = True
    if effective_search_space is None:
        if split_query_search_spaces:
            required["query_split"] = "one split child job per effective search-space group"
        elif query_count == 1:
            blockers.append(
                "precise single-query sharding requires db_effective_search_space "
                "or one query_effective_search_spaces value"
            )
            required["db_effective_search_space"] = "positive integer"
        elif query_count is None:
            blockers.append("precise sharding requires effective search space metadata")
            required["db_effective_search_space"] = "positive integer"

    return PrecisionReport(
        requested_mode=mode,
        effective_mode="precise" if not blockers else "off",
        precision_level=(
            "precise_single_query"
            if not blockers and query_count == 1
            else "precise_xml_split"
            if not blockers and split_query_search_spaces and merge_format == "xml"
            else "precise_tabular_split"
            if not blockers and split_query_search_spaces
            else "precise_xml"
            if not blockers and merge_format == "xml"
            else "precise_tabular"
            if not blockers
            else "blocked"
        ),
        eligible=not blockers,
        merge_strategy=(
            "query_group_split_xml_top_n"
            if split_query_search_spaces and merge_format == "xml"
            else "query_group_split_tabular_top_n"
            if split_query_search_spaces
            else "xml_top_n"
            if merge_format == "xml"
            else "tabular_top_n"
        ),
        required_options=required,
        blocking_errors=blockers,
        warnings=warnings,
    )
