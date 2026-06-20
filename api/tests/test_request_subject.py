"""Tests for the natural Service Bus request Subject builder.

Responsibility: Verify ``build_request_subject`` composes a distinguishable
    Subject from program/db/query identity and falls back to the historical
    ``blast.request`` constant when nothing meaningful exists, and that it never
    raises on a malformed body.
Edit boundaries: Test-only; pure function under test, no Service Bus / Redis.
Key entry points: ``test_*`` functions below.
Risky contracts: The Subject is display-only; the consumer never routes on it,
    so these tests pin readability + the safe fallback, not message routing.
Validation: ``uv run pytest -q api/tests/test_request_subject.py``.
"""

from __future__ import annotations

from api.services.blast.request_subject import (
    DEFAULT_REQUEST_SUBJECT,
    build_request_subject,
)


def test_subject_combines_program_db_and_single_query() -> None:
    subject = build_request_subject(
        {"program": "blastn", "db": "core_nt", "query_fasta": ">sp|P12345 desc\nACGT"}
    )
    assert subject == "blastn core_nt \u00b7 sp|P12345"


def test_subject_marks_multi_record_query() -> None:
    subject = build_request_subject(
        {
            "program": "blastp",
            "db": "nr",
            "query_fasta": ">q1\nMAAA\n>q2\nMBBB\n>q3\nMCCC",
        }
    )
    assert subject == "blastp nr \u00b7 q1 (+2)"


def test_subject_without_query_uses_program_and_db_only() -> None:
    assert build_request_subject({"program": "blastn", "db": "core_nt"}) == "blastn core_nt"


def test_subject_with_program_only() -> None:
    assert build_request_subject({"program": "blastx"}) == "blastx"


def test_subject_falls_back_when_nothing_meaningful() -> None:
    assert build_request_subject({}) == DEFAULT_REQUEST_SUBJECT
    assert build_request_subject({"options": {"outfmt": 5}}) == DEFAULT_REQUEST_SUBJECT
    assert build_request_subject(None) == DEFAULT_REQUEST_SUBJECT


def test_subject_fasta_without_defline_falls_back_to_head() -> None:
    # A FASTA body with no '>' header yields no label, so only program/db remain.
    assert (
        build_request_subject({"program": "blastn", "db": "nt", "query_fasta": "ACGTACGT"})
        == "blastn nt"
    )


def test_subject_never_raises_on_bad_body() -> None:
    # Non-dict body must not raise and falls back to the constant; a non-string
    # program is stringified rather than crashing (real paths pass validated
    # Pydantic models, so this only pins the no-raise contract).
    assert build_request_subject([]) == DEFAULT_REQUEST_SUBJECT  # type: ignore[arg-type]
    assert isinstance(build_request_subject({"program": 123, "db": None}), str)


def test_subject_is_length_bounded() -> None:
    long_id = "x" * 300
    subject = build_request_subject(
        {"program": "blastn", "db": "nt", "query_fasta": f">{long_id}\nACGT"}
    )
    assert len(subject) <= 120
