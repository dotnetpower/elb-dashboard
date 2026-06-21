"""Unit + HTTP tests for the BLAST database selection oracle.

Responsibility: Verify the recommendation rule table and the
`/api/blast/databases/recommend` route.
Edit boundaries: Keep assertions focused on recommendation logic; no live Azure.
Key entry points: `test_dna_identify_recommends_core_nt`,
`test_recommend_route_returns_recommendation`.
Risky contracts: Do not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_blast_db_recommendation.py`.
"""

from __future__ import annotations

from api.services.blast.db_recommendation import (
    RECOMMENDATION_RULESET_VERSION,
    program_database_compatibility_error,
    recommend_database,
)
from fastapi.testclient import TestClient


def test_dna_identify_recommends_core_nt() -> None:
    rec = recommend_database(molecule="dna", goal="identify")
    assert rec.recommended.db == "core_nt"
    assert rec.alternative.db == "nt"
    assert rec.molecule == "dna"
    assert rec.ruleset_version == RECOMMENDATION_RULESET_VERSION


def test_protein_well_characterized_recommends_swissprot() -> None:
    rec = recommend_database(molecule="protein", goal="well_characterized")
    assert rec.recommended.db == "swissprot"
    assert rec.alternative.db == "refseq_protein"


def test_program_blastx_treated_as_protein_db() -> None:
    rec = recommend_database(program="blastx", goal="identify")
    assert rec.molecule == "protein"
    assert rec.recommended.db == "nr"


def test_program_blastn_treated_as_dna_db() -> None:
    rec = recommend_database(program="blastn", goal="transcripts")
    assert rec.molecule == "dna"
    assert rec.recommended.db == "refseq_rna"


def test_unknown_goal_falls_back_to_identify() -> None:
    rec = recommend_database(molecule="dna", goal="nonsense")
    assert rec.goal == "identify"
    assert rec.recommended.db == "core_nt"


def test_protein_transcripts_falls_back_gracefully() -> None:
    rec = recommend_database(molecule="protein", goal="transcripts")
    # Transcripts is nucleotide-only; protein falls back to identify.
    assert rec.recommended.db == "nr"


def test_taxon_adds_filter_note_not_db_switch() -> None:
    rec = recommend_database(molecule="dna", goal="identify", taxon="9606")
    assert rec.recommended.db == "core_nt"
    assert any("9606" in note for note in rec.notes)
    assert any("taxid" in note.lower() for note in rec.notes)


def test_recommend_route_returns_recommendation(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/databases/recommend",
        params={"program": "blastp", "goal": "comprehensive"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["recommended"]["db"] == "nr"
    assert body["alternative"]["db"] == "refseq_protein"
    assert body["ruleset_version"] == RECOMMENDATION_RULESET_VERSION
    assert "rationale" in body["recommended"]


def test_program_database_compatibility_blocks_known_mismatch() -> None:
    """A known program against a known opposite-molecule DB is blocked."""
    # blastp (protein) against a nucleotide DB -> blocked.
    err = program_database_compatibility_error("blastp", "core_nt")
    assert err is not None
    assert "nucleotide" in err and "core_nt" in err
    # blastn (nucleotide) against a protein DB -> blocked.
    assert program_database_compatibility_error("blastn", "nr") is not None
    # tblastn needs a nucleotide DB; nr is protein -> blocked.
    assert program_database_compatibility_error("tblastn", "nr") is not None
    # URL-form database value is normalised via extract_db_name.
    url = "https://acct.blob.core.windows.net/blast-db/core_nt/core_nt"
    assert program_database_compatibility_error("blastp", url) is not None


def test_program_database_compatibility_allows_valid_and_unknown() -> None:
    """Valid pairings AND any unknown side are allowed (no false rejects)."""
    # Valid pairings.
    assert program_database_compatibility_error("blastn", "core_nt") is None
    assert program_database_compatibility_error("blastp", "nr") is None
    assert program_database_compatibility_error("blastx", "nr") is None  # protein DB
    assert program_database_compatibility_error("tblastn", "core_nt") is None  # nucl DB
    assert program_database_compatibility_error("rpsblast", "cdd") is None
    # Unknown database (custom BLAST DB) -> never rejected.
    assert program_database_compatibility_error("blastp", "my_custom_db") is None
    # Unknown program (future BLAST+ addition) -> never rejected.
    assert program_database_compatibility_error("futureblast", "core_nt") is None
    # Empty inputs -> allowed.
    assert program_database_compatibility_error("", "") is None
    assert program_database_compatibility_error("blastp", "") is None
