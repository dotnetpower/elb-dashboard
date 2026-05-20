from __future__ import annotations

from api.services.blast_equivalence_evidence import (
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
