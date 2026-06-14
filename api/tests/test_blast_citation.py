"""Unit + HTTP tests for the BLAST run citation builder and route.

Responsibility: Verify citation rendering across formats and the
`/api/blast/jobs/{id}/citation` route's auth + payload handling.
Edit boundaries: Keep tests focused on citation behaviour; use fakes instead of
live Azure calls.
Key entry points: `test_build_citation_text_includes_program_and_db`,
`test_citation_route_returns_requested_format`.
Risky contracts: Do not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_blast_citation.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.blast.citation import build_citation
from fastapi.testclient import TestClient

_PROVENANCE = {
    "schema_version": 1,
    "job_id": "job-cite-1",
    "blast": {"program": "blastn", "version": "2.17.0+"},
    "database": {
        "name": "core_nt",
        "input": "core_nt",
        "snapshot": "2026-05-01",
        "search_space": "1234567890",
    },
    "options": {"evalue": "1e-5", "matrix": "BLOSUM62", "max_target_seqs": 50},
}


def test_build_citation_text_includes_program_and_db() -> None:
    bundle = build_citation(job_id="job-cite-1", provenance=_PROVENANCE, job_title="My run")
    assert "BLAST+" in bundle.text
    assert "blastn" in bundle.text
    assert "2.17.0+" in bundle.text
    assert "core_nt" in bundle.text
    assert "2026-05-01" in bundle.text
    assert "ELB-job-cite-1" == bundle.rid
    assert "My run" in bundle.text
    # Options surfaced in the Methods sentence.
    assert "1e-5" in bundle.text
    assert "BLOSUM62" in bundle.text
    assert "50 target sequences" in bundle.text


def test_build_citation_markdown_and_bibtex_formats() -> None:
    bundle = build_citation(job_id="job-cite-1", provenance=_PROVENANCE)
    assert bundle.render("markdown").startswith("Sequence similarity")
    assert "**References**" in bundle.render("markdown")
    bibtex = bundle.render("bibtex")
    assert "@article{camacho2009blast" in bibtex
    assert "@article{boratyn2023elasticblast" in bibtex
    assert "@misc{elbdashboard_run_jobcite1" in bibtex


def test_build_citation_degrades_without_provenance() -> None:
    bundle = build_citation(job_id="job-x", provenance=None)
    assert bundle.program == "blastn"
    assert bundle.blast_version == "unknown"
    assert "default search parameters" in bundle.text


def test_build_citation_without_db_name_renders_single_clause() -> None:
    """When the provenance lacks a database name (external-API job with no
    canonical snapshot), the Methods paragraph must read 'queried the selected
    database' once -- not the 'queried the the selected database database'
    duplicate that the old fallback produced (issue #8)."""
    provenance = {
        "schema_version": 1,
        "job_id": "job-no-db",
        "blast": {"program": "blastn", "version": "2.17.0+"},
        "database": {},  # no name, no input
        "options": {},
    }
    bundle = build_citation(job_id="job-no-db", provenance=provenance)
    assert "the the" not in bundle.text
    assert "database database" not in bundle.text
    assert "queried the selected database" in bundle.text
    assert "the the" not in bundle.markdown
    assert "database database" not in bundle.markdown


def test_citation_never_emits_storage_urls() -> None:
    bundle = build_citation(job_id="job-cite-1", provenance=_PROVENANCE)
    for blob in (bundle.text, bundle.markdown, bundle.bibtex):
        assert "https://" not in blob or "doi.org" in blob or "://" not in blob
        assert "sig=" not in blob
        assert "blob.core.windows.net" not in blob


def _fake_repo(payload: dict, *, owner_oid: str):
    state = SimpleNamespace(
        job_id="job-cite-1",
        owner_oid=owner_oid,
        job_title="My run",
        payload=payload,
    )

    class Repo:
        def get(self, job_id: str):
            assert job_id == "job-cite-1"
            return state

    return Repo()


def test_citation_route_returns_requested_format(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    owner_oid = "00000000-0000-0000-0000-000000000000"
    repo = _fake_repo({"provenance": _PROVENANCE}, owner_oid=owner_oid)
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-cite-1/citation", params={"format": "bibtex"})
    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "bibtex"
    assert "@article{camacho2009blast" in body["citation"]
    assert body["rid"] == "ELB-job-cite-1"
    assert body["program"] == "blastn"
    assert body["database"] == "core_nt"


def test_citation_route_rejects_non_owner(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    repo = _fake_repo({"provenance": _PROVENANCE}, owner_oid="someone-else")
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-cite-1/citation")
    assert response.status_code == 403


def test_citation_route_404_when_missing(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class Repo:
        def get(self, job_id: str):
            return None

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: Repo())

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-missing/citation")
    assert response.status_code == 404
