"""Tests for the BLAST workflow-manager export (roadmap R3, issue #57).

Responsibility: cover the offline workflow renderer service
(`api/services/blast/workflow_export.py`) and the owner-scoped
`GET /api/blast/jobs/{job_id}/export` route, asserting the pinned-parameter
contract, the secret-free / idempotency-safe invariants, and per-format markers.
Edit boundaries: pure unit + TestClient tests; no Azure or network calls.
Key entry points: `test_*` functions.
Risky contracts: locks "never pin idempotency_key" and "never embed a token".
Validation: `uv run pytest -q api/tests/test_blast_workflow_export.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.services.blast.workflow_export import (
    SUPPORTED_WORKFLOW_FORMATS,
    MissingDatabaseError,
    UnsupportedFormatError,
    build_pinned_request,
    render_workflow_export,
)
from fastapi.testclient import TestClient

_OWNER = "00000000-0000-0000-0000-000000000000"

_SNAPSHOT = {
    "program": "tblastn",
    "database": "core_nt",
    "options": {
        "evalue": 0.05,
        "word_size": 28,
        "max_target_seqs": 500,
        "low_complexity_filter": True,
        "sharding_mode": "off",
    },
}


# --------------------------------------------------------------------------- #
# Service-level unit tests
# --------------------------------------------------------------------------- #
def test_build_pinned_request_pins_core_params_without_query() -> None:
    body = build_pinned_request(_SNAPSHOT)
    assert body["program"] == "tblastn"
    assert body["db"] == "core_nt"
    # The query FASTA is a runtime input, never pinned.
    assert "query_fasta" not in body
    # Options mapped onto the ExternalBlastOptions subset.
    assert body["options"]["evalue"] == 0.05
    assert body["options"]["word_size"] == 28
    assert body["options"]["max_target_seqs"] == 500
    assert body["options"]["dust"] is True
    assert body["options"]["sharding_mode"] == "off"


def test_build_pinned_request_never_pins_idempotency_key() -> None:
    snap = {**_SNAPSHOT, "idempotency_key": "abc", "external_correlation_id": "xyz"}
    body = build_pinned_request(snap)
    assert "idempotency_key" not in body
    assert "external_correlation_id" not in body


def test_build_pinned_request_requires_database() -> None:
    with pytest.raises(MissingDatabaseError):
        build_pinned_request({"program": "blastn"})


def test_build_pinned_request_pins_taxid_when_present() -> None:
    body = build_pinned_request({**_SNAPSHOT, "taxid": 3431483, "is_inclusive": False})
    assert body["taxid"] == 3431483
    assert body["is_inclusive"] is False


def test_build_pinned_request_ignores_invalid_taxid() -> None:
    body = build_pinned_request({**_SNAPSHOT, "taxid": 0})
    assert "taxid" not in body


@pytest.mark.parametrize("fmt", SUPPORTED_WORKFLOW_FORMATS)
def test_render_contains_pinned_params_and_no_secrets(fmt: str) -> None:
    export = render_workflow_export(job_id="job-1", snapshot=_SNAPSHOT, fmt=fmt)
    content = export.content
    # Pinned identity is present.
    assert "core_nt" in content
    assert "tblastn" in content
    # Targets the canonical public submit path.
    assert "/api/blast/jobs" in content
    # Token + base URL come from the environment, never embedded.
    assert "ELB_TOKEN" in content
    assert "ELB_BASE_URL" in content
    # No literal bearer token value leaked.
    assert "Bearer eyJ" not in content
    assert export.media_type.startswith("text/plain")


@pytest.mark.parametrize("fmt", SUPPORTED_WORKFLOW_FORMATS)
def test_render_submit_call_has_bounded_timeout(fmt: str) -> None:
    """Generated workflow MUST bound the POST so a control-plane outage fails
    the pipeline step in seconds, not hangs (urllib defaults to no timeout)."""
    content = render_workflow_export(job_id="j", snapshot=_SNAPSHOT, fmt=fmt).content
    assert "urlopen(req, timeout=" in content
    assert "ELB_SUBMIT_TIMEOUT" in content


def test_render_format_markers() -> None:
    assert "nextflow.enable.dsl=2" in render_workflow_export(
        job_id="j", snapshot=_SNAPSHOT, fmt="nextflow"
    ).content
    assert "rule blast_submit:" in render_workflow_export(
        job_id="j", snapshot=_SNAPSHOT, fmt="snakemake"
    ).content
    assert "cwlVersion: v1.2" in render_workflow_export(
        job_id="j", snapshot=_SNAPSHOT, fmt="cwl"
    ).content
    assert "version 1.0" in render_workflow_export(
        job_id="j", snapshot=_SNAPSHOT, fmt="wdl"
    ).content


def test_render_filenames() -> None:
    names = {
        fmt: render_workflow_export(job_id="j", snapshot=_SNAPSHOT, fmt=fmt).filename
        for fmt in SUPPORTED_WORKFLOW_FORMATS
    }
    assert names == {
        "nextflow": "main.nf",
        "snakemake": "Snakefile",
        "cwl": "blast_submit.cwl",
        "wdl": "blast_submit.wdl",
    }


def test_render_rejects_unknown_format() -> None:
    with pytest.raises(UnsupportedFormatError):
        render_workflow_export(job_id="j", snapshot=_SNAPSHOT, fmt="airflow")


# --------------------------------------------------------------------------- #
# Route tests
# --------------------------------------------------------------------------- #
def _repo_with(state) -> type:
    class Repo:
        def get(self, job_id: str):
            return state

    return Repo


def _state(*, owner: str = _OWNER, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job-1",
        owner_oid=owner,
        payload=payload if payload is not None else {"canonical_request": _SNAPSHOT},
    )


@pytest.mark.parametrize("fmt", SUPPORTED_WORKFLOW_FORMATS)
def test_export_route_returns_file(monkeypatch, fmt: str) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with(_state())()
    )

    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/blast/jobs/job-1/export", params={"format": fmt})

    assert resp.status_code == 200
    assert "core_nt" in resp.text
    assert "tblastn" in resp.text
    expected = {
        "nextflow": "main.nf",
        "snakemake": "Snakefile",
        "cwl": "blast_submit.cwl",
        "wdl": "blast_submit.wdl",
    }[fmt]
    assert expected in resp.headers["content-disposition"]
    assert resp.headers["x-elb-export-format"] == fmt


def test_export_route_defaults_to_nextflow(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with(_state())()
    )

    from api.main import app

    resp = TestClient(app).get("/api/blast/jobs/job-1/export")
    assert resp.status_code == 200
    assert "main.nf" in resp.headers["content-disposition"]


def test_export_route_rejects_unknown_format(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with(_state())()
    )

    from api.main import app

    resp = TestClient(app).get("/api/blast/jobs/job-1/export", params={"format": "airflow"})
    assert resp.status_code == 422


def test_export_route_missing_job_404(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with(None)()
    )

    from api.main import app

    resp = TestClient(app).get("/api/blast/jobs/missing/export")
    assert resp.status_code == 404


def test_export_route_non_owner_403(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    other = _state(owner="11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with(other)()
    )

    from api.main import app

    resp = TestClient(app).get("/api/blast/jobs/job-1/export")
    assert resp.status_code == 403


def test_export_route_no_database_422(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    state = _state(payload={"canonical_request": {"program": "blastn", "database": ""}})
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: _repo_with(state)()
    )

    from api.main import app

    resp = TestClient(app).get("/api/blast/jobs/job-1/export")
    assert resp.status_code == 422
    assert resp.json()["code"] == "export_unavailable"
