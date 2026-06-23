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


def outfmt_spec_value(options: str) -> str | None:
    """Return the FULL ``-outfmt`` specifier (code + field codes) or ``None``.

    Unlike :func:`option_value` (which returns only the single token after the
    flag, i.e. the leading numeric code), this rejoins every token after
    ``-outfmt`` up to the next ``-flag`` so an extended layout like
    ``-outfmt 7 qseqid sseqid staxids evalue bitscore`` is returned whole. This
    mirrors ``terminal/merge-sharded-results.sh::parse_outfmt_spec`` so the
    submit-time sharding gate can validate the same field list the runtime merge
    will resolve. Handles both the UNQUOTED multi-token form (separate tokens)
    and a quoted single-token form.
    """
    try:
        tokens = shlex.split(options or "")
    except ValueError as exc:
        raise ValueError(f"invalid blast options: {exc}") from exc
    spec: str | None = None
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-outfmt" and i + 1 < n:
            parts: list[str] = []
            j = i + 1
            while j < n and not tokens[j].startswith("-"):
                parts.append(tokens[j])
                j += 1
            spec = " ".join(parts)
            i = j
            continue
        if tok.startswith("-outfmt="):
            spec = tok.split("=", 1)[1]
        i += 1
    return spec.strip() if spec is not None else None


def outfmt_is_merge_compatible(value: object | None) -> bool:
    """Return True for output layouts the shard merge engine supports."""
    return merge_format_for_outfmt(value) is not None


# Standard 12-column BLAST tabular layout (the ``std`` token), mirrored from
# ``terminal/merge-sharded-results.sh::_STD_TABULAR_FIELDS`` so the submit-time
# gate and the runtime merge agree on which fields ``std`` expands to.
_STD_TABULAR_FIELDS: tuple[str, ...] = (
    "qseqid", "sseqid", "pident", "length", "mismatch", "gapopen",
    "qstart", "qend", "sstart", "send", "evalue", "bitscore",
)


def _expand_outfmt_field_codes(field_tokens: list[str]) -> list[str]:
    """Expand a tabular outfmt field list, replacing ``std`` with its 12 codes.

    Mirrors ``expand_outfmt_fields`` in the merge script. Field codes are
    lower-cased so the ``evalue`` / ``bitscore`` presence check is
    case-insensitive.
    """
    fields: list[str] = []
    for tok in field_tokens:
        if tok == "std":
            fields.extend(_STD_TABULAR_FIELDS)
        else:
            fields.append(tok.lower())
    return fields


def merge_format_for_outfmt(value: object | None) -> Literal["tabular", "xml"] | None:
    """Return the supported shard merge family for a BLAST outfmt value.

    outfmt 7 is the same 12-column tabular layout as outfmt 6 with added
    comment lines; the shard merge skips the comment lines and re-emits its
    own, so 7 merges via the same tabular path as 6 (plain or ``std``).

    Extended / reordered tabular layouts (e.g.
    ``7 qseqid sseqid staxids sstrand pident evalue bitscore``) are accepted as
    long as the expanded field list carries both ``evalue`` and ``bitscore`` —
    the shard merge resolves its group/rank columns by NAME and cannot re-rank
    shard hits without those two (it raises ``ValueError`` otherwise). Requiring
    them here surfaces a malformed layout at submit time instead of ~minutes
    later when the finalizer merge runs. ``qseqid`` is optional (a missing query
    column makes the merge treat every hit as one query group, correct for a
    single-query search) so it is not required by this gate. This mirrors
    ``terminal/merge-sharded-results.sh::resolve_tabular_columns``.
    """
    if value in (None, ""):
        return "tabular"
    parts = str(value).strip().strip("'\"").split()
    if not parts:
        return "tabular"
    if parts[0] == "5" and len(parts) == 1:
        return "xml"
    if parts[0] in ("6", "7"):
        if len(parts) == 1:
            return "tabular"
        fields = _expand_outfmt_field_codes(parts[1:])
        if "evalue" in fields and "bitscore" in fields:
            return "tabular"
        return None
    return None


# Web BLAST parity columns the dashboard result analytics need to populate
# Description (`stitle`), Scientific name (`sscinames` / `staxids`), and Query
# Cover (`qcovs`). The default tabular layout (`std`, or a bare `6` / `7`) omits
# all of them, so an outfmt 6/7 run would show blanks on the result page where
# an outfmt 5 (XML) run is rich. Injecting them at submit time closes that gap.
# Resolved BY NAME by both the shard merge (`merge-sharded-results.sh`, which
# re-emits trailing columns) and the analytics parser, so order does not matter.
_PARITY_TABULAR_FIELDS: tuple[str, ...] = ("staxids", "sscinames", "stitle", "qcovs")


def enrich_tabular_outfmt(value: object | None) -> object | None:
    """Append the result-UI parity columns to a tabular (6/7) outfmt, idempotently.

    Returns ``value`` unchanged for XML (``5``), a non-tabular code, or a value
    that already lists every parity column. A bare ``6`` / ``7`` is expanded to
    ``std`` first so ``evalue`` + ``bitscore`` (required by the shard merge) stay
    present. Already-present columns are never duplicated, so re-running this on
    an enriched value is a no-op.
    """
    if value in (None, ""):
        return value
    parts = str(value).strip().strip("'\"").split()
    if not parts or parts[0] not in ("6", "7"):
        return value
    code = parts[0]
    cols = parts[1:] or ["std"]
    present = set(_expand_outfmt_field_codes(cols))
    additions = [f for f in _PARITY_TABULAR_FIELDS if f not in present]
    if not additions and parts[1:]:
        return value
    return " ".join([code, *cols, *additions])


def set_outfmt_spec(options: str, spec: str) -> str:
    """Return ``options`` with its ``-outfmt`` specifier replaced by ``spec``.

    Drops any existing ``-outfmt <code> [fields...]`` run (and the ``-outfmt=``
    form) and appends ``-outfmt <spec>`` as separate UNQUOTED tokens at the end.
    Mirrors the tokenisation in :func:`outfmt_spec_value`; the unquoted multi-
    token form is required because the generated elastic-blast.ini rejects a
    quoted ``-outfmt`` value (it breaks the K8s Job YAML the sibling renders).
    """
    try:
        tokens = shlex.split(options or "")
    except ValueError as exc:
        raise ValueError(f"invalid blast options: {exc}") from exc
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-outfmt":
            j = i + 1
            while j < n and not tokens[j].startswith("-"):
                j += 1
            i = j
            continue
        if tok.startswith("-outfmt="):
            i += 1
            continue
        out.append(tok)
        i += 1
    out.append("-outfmt")
    out.extend(str(spec).split())
    return " ".join(out)


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
            "sharded result merge currently supports only outfmt 5, outfmt 6, "
            "outfmt 7, or outfmt '6 std...'/'7 std...'"
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
