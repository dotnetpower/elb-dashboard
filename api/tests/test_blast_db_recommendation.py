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
