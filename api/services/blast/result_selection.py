"""Validate opt-in BLAST result-selection request options.

Responsibility: Validate sequence-diversity outfmt fields and candidate-pool bounds.
Edit boundaries: Keep request semantics here; sibling option rewriting and runtime merge logic
    remain in the OpenAPI execution plane.
Key entry points: `validate_result_selection_options`, `SequenceDiversityPlan`,
    `SequenceDiversityValidationError`.
Risky contracts: Validation uses the server-enriched effective outfmt, never adds caller-semantic
    fields, and leaves existing policies byte-for-byte unchanged.
Validation: `uv run pytest -q api/tests/test_servicebus_v1_multitoken.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ResultSelectionPolicy = Literal[
    "native_top_n", "diversity_aware", "sequence_diversity"
]

SEQUENCE_DIVERSITY_DEFAULT_CANDIDATE_POOL_SIZE = 2_000
SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE = 5_000
SEQUENCE_IDENTITY_MODE = "aligned_sequence_query_span"
SEQUENCE_IDENTITY_VERSION = 1

_STD_TABULAR_FIELDS = (
    "qseqid",
    "sseqid",
    "pident",
    "length",
    "mismatch",
    "gapopen",
    "qstart",
    "qend",
    "sstart",
    "send",
    "evalue",
    "bitscore",
)
_QUERY_FIELDS = frozenset({"qseqid", "qacc", "qaccver", "qgi"})
_ACCESSION_FIELDS = frozenset({"sseqid", "sacc", "saccver", "sgi"})
_REQUIRED_FIELDS = (
    "query_identity",
    "accession",
    "sseq",
    "qstart",
    "qend",
    "evalue",
    "bitscore",
    "score",
)


class SequenceDiversityValidationError(ValueError):
    """A safe, machine-readable permanent sequence-diversity rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        missing_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.missing_fields = missing_fields

    def detail(self) -> dict[str, object]:
        detail: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": False,
        }
        if self.missing_fields:
            detail["missing_fields"] = list(self.missing_fields)
        return detail


@dataclass(frozen=True)
class SequenceDiversityPlan:
    """Validated group target and per-shard candidate cap."""

    requested_sequence_groups: int
    candidate_pool_size_requested_per_shard: int | None
    candidate_pool_size_applied_per_shard: int


def _effective_fields(outfmt: object | None) -> tuple[str, ...]:
    parts = str(outfmt or "").strip().strip("'\"").split()
    if not parts or parts[0] not in {"6", "7"}:
        raise SequenceDiversityValidationError(
            "sequence_diversity_invalid_outfmt",
            "sequence_diversity supports only tabular BLAST outfmt 6 or 7",
        )
    fields: list[str] = []
    for field in parts[1:] or ["std"]:
        if field.lower() == "std":
            fields.extend(_STD_TABULAR_FIELDS)
        else:
            fields.append(field.lower())
    return tuple(fields)


def _missing_fields(fields: tuple[str, ...]) -> tuple[str, ...]:
    present = set(fields)
    missing: list[str] = []
    for required in _REQUIRED_FIELDS:
        if required == "query_identity":
            available = bool(present & _QUERY_FIELDS)
        elif required == "accession":
            available = bool(present & _ACCESSION_FIELDS)
        else:
            available = required in present
        if not available:
            missing.append(required)
    return tuple(missing)


def validate_result_selection_options(
    *,
    policy: ResultSelectionPolicy,
    effective_outfmt: object | None,
    max_target_seqs: int | None,
    candidate_pool_size: int | None,
) -> SequenceDiversityPlan | None:
    """Validate one request after server-side tabular score enrichment."""
    if policy != "sequence_diversity":
        if candidate_pool_size is not None:
            raise SequenceDiversityValidationError(
                "sequence_diversity_invalid_candidate_pool",
                "candidate_pool_size is valid only for sequence_diversity",
            )
        return None

    requested_groups = 500 if max_target_seqs is None else max_target_seqs
    if (
        isinstance(requested_groups, bool)
        or not isinstance(requested_groups, int)
        or requested_groups <= 0
        or requested_groups > SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE
    ):
        raise SequenceDiversityValidationError(
            "sequence_diversity_invalid_candidate_pool",
            "sequence_diversity max_target_seqs must be between 1 and 5000",
        )

    requested_pool = candidate_pool_size
    if requested_pool is None:
        applied_pool = max(
            SEQUENCE_DIVERSITY_DEFAULT_CANDIDATE_POOL_SIZE,
            requested_groups,
        )
    elif isinstance(requested_pool, bool) or not isinstance(requested_pool, int):
        raise SequenceDiversityValidationError(
            "sequence_diversity_invalid_candidate_pool",
            "candidate_pool_size must be a positive integer",
        )
    else:
        applied_pool = requested_pool

    if applied_pool <= 0 or applied_pool > SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE:
        raise SequenceDiversityValidationError(
            "sequence_diversity_invalid_candidate_pool",
            "candidate_pool_size must be between 1 and 5000",
        )
    if applied_pool < requested_groups:
        raise SequenceDiversityValidationError(
            "sequence_diversity_invalid_candidate_pool",
            "candidate_pool_size must be greater than or equal to max_target_seqs",
        )

    missing = _missing_fields(_effective_fields(effective_outfmt))
    if missing:
        raise SequenceDiversityValidationError(
            "sequence_diversity_missing_fields",
            "sequence_diversity requires query identity, accession, sseq, qstart, "
            "qend, evalue, bitscore, and score in the effective tabular outfmt",
            missing_fields=missing,
        )

    return SequenceDiversityPlan(
        requested_sequence_groups=requested_groups,
        candidate_pool_size_requested_per_shard=requested_pool,
        candidate_pool_size_applied_per_shard=applied_pool,
    )
