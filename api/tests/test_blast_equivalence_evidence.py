"""Tests for BLAST Equivalence Evidence behavior.

Responsibility: Tests for BLAST Equivalence Evidence behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_verified_search_space_registry_requires_evidence_metadata`,
`test_evidence_registry_matrix_includes_core_nt`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_equivalence_evidence.py`.
"""

from __future__ import annotations

from api.services.blast.equivalence_evidence import (
    evidence_registry_matrix,
    validate_evidence_registry,
)


def test_verified_search_space_registry_requires_evidence_metadata() -> None:
    assert validate_evidence_registry() == []


def test_evidence_registry_matrix_includes_core_nt() -> None:
    matrix = evidence_registry_matrix()

    core_nt = next(row for row in matrix if row["db_name"] == "core_nt")
    assert core_nt["value"] == 32_156_241_807_668
    assert core_nt["blast_version"] == "BLASTN 2.17.0+"
    assert "core_nt 2026-05-09" in core_nt["database_snapshot"]
