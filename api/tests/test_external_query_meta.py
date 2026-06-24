"""Tests for the FASTA query-identity helper (length + molecule).

Validation: ``uv run pytest -q api/tests/test_external_query_meta.py``.
"""

from __future__ import annotations

from api.services.blast.external_query_meta import query_meta_from_fasta


def test_nucleotide_query() -> None:
    meta = query_meta_from_fasta(">q1\nACGTACGTACGTNNNN")
    assert meta["length"] == 16
    assert meta["records"] == 1
    assert meta["molecule"] == "nucleotide"


def test_protein_query() -> None:
    meta = query_meta_from_fasta(">p1\nMKLVWXYZEFHIQRP")
    assert meta["molecule"] == "protein"
    assert meta["length"] == 15


def test_multi_record_totals_length() -> None:
    meta = query_meta_from_fasta(">a\nACGT\n>b\nACGTACGT")
    assert meta["records"] == 2
    assert meta["length"] == 12
    assert meta["molecule"] == "nucleotide"


def test_empty_and_invalid_inputs() -> None:
    assert query_meta_from_fasta("") == {}
    assert query_meta_from_fasta(None) == {}  # type: ignore[arg-type]
    assert query_meta_from_fasta(">header-only\n") == {}
    assert query_meta_from_fasta(123) == {}  # type: ignore[arg-type]


def test_whitespace_in_sequence_is_stripped() -> None:
    meta = query_meta_from_fasta(">q\nACG TAC GT")
    assert meta["length"] == 8
