"""Tests for portable BLAST reproducibility packages and their download route.

Responsibility: Verify package composition, secret/replay-identity exclusion, optional manifest
    degradation, and owner-scoped HTTP download behavior.
Edit boundaries: Pure service and TestClient tests only; no real Azure, Storage, or broker calls.
Key entry points: ``test_*``.
Risky contracts: Packages must not contain raw FASTA, SAS material, bearer tokens, idempotency
    keys, or external correlation ids.
Validation: ``uv run pytest -q api/tests/test_blast_reproducibility.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from api.services.blast.reproducibility import build_reproducibility_package
from fastapi.testclient import TestClient

_OWNER = "00000000-0000-0000-0000-000000000000"
_PAYLOAD = {
    "program": "blastn",
    "db": "core_nt",
    "query_data": ">private\nACGT",
    "canonical_request": {
        "schema_version": 1,
        "program": "blastn",
        "database": "core_nt",
        "query": {"kind": "inline", "sha256": "abc", "total_letters": 4},
        "options": {"evalue": 0.05, "max_target_seqs": 500},
        "metadata": {
            "idempotency_key": "do-not-replay",
            "external_correlation_id": "do-not-replay-either",
        },
    },
}


def _state(*, owner_oid: str = _OWNER) -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job-repro-1",
        owner_oid=owner_oid,
        job_title="Reference run",
        program="blastn",
        db="core_nt",
        status="completed",
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:05:00+00:00",
        payload=_PAYLOAD,
    )


def test_package_is_portable_and_excludes_replay_and_query_material() -> None:
    package = build_reproducibility_package(
        state=_state(),
        result_manifest={"artifact_schema_version": 1, "files": [{"name": "result.xml"}]},
        generated_at="2026-09-09T00:00:00+00:00",
    ).model_dump(mode="json")

    assert package["schema_version"] == 1
    assert package["job"]["job_id"] == "job-repro-1"
    assert package["availability"] == {
        "result_manifest": True,
        "workflow_exports": True,
    }
    assert set(package["workflow_exports"]) == {"nextflow", "snakemake", "cwl", "wdl"}
    encoded = json.dumps(package)
    assert ">private" not in encoded
    assert "do-not-replay" not in encoded
    assert "external_correlation_id" not in encoded
    assert "sig=" not in encoded.casefold()
    assert package["raw_query_included"] is False


def test_package_degrades_when_database_and_manifest_are_missing() -> None:
    state = _state()
    state.db = ""
    state.payload = {"program": "blastn"}

    package = build_reproducibility_package(
        state=state,
        generated_at="2026-09-09T00:00:00+00:00",
    )

    assert package.result_manifest is None
    assert package.workflow_exports == {}
    assert package.availability == {
        "result_manifest": False,
        "workflow_exports": False,
    }


def test_package_redacts_sensitive_legacy_metadata_and_storage_urls() -> None:
    state = _state()
    state.payload = {
        **_PAYLOAD,
        "canonical_request": {
            **_PAYLOAD["canonical_request"],
            "query": {
                "kind": "query_file",
                "path": "https://acct.blob.core.windows.net/queries/private.fa?sv=1&sp=r&sig=secret",
                "query_data": ">private\nACGT",
            },
            "metadata": {"access_token": "Bearer private-token-value-1234567890"},
            "legacy_query": ">private\nACGTACGT",
        },
    }

    package = build_reproducibility_package(
        state=state,
        result_manifest={
            "files": [
                {
                    "name": "https://acct.blob.core.windows.net/results/out.xml?sv=1&sp=r&sig=secret"
                }
            ],
            "client_secret": "private-secret-value",
        },
        generated_at="2026-09-09T00:00:00+00:00",
    ).model_dump(mode="json")

    encoded = json.dumps(package)
    assert ">private" not in encoded
    assert "private-token" not in encoded
    assert "private-secret" not in encoded
    assert "sig=" not in encoded
    assert "acct.blob.core.windows.net" not in encoded
    assert package["submit_snapshot"]["legacy_query"] == "<query-sequence-redacted>"
    assert package["submit_snapshot"]["query"]["path"] == "queries/private.fa"
    assert package["result_manifest"]["files"][0]["name"] == "results/out.xml"


class _Repo:
    def __init__(self, state: SimpleNamespace | None) -> None:
        self.state = state

    def get(self, job_id: str) -> SimpleNamespace | None:
        assert job_id in {"job-repro-1", "missing"}
        return self.state


def test_reproducibility_route_returns_download(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Repo(_state()))
    monkeypatch.setattr(
        "api.services.job_artifacts.read_json_artifact",
        lambda *_args, **_kwargs: {"artifact_schema_version": 1, "files": []},
    )

    from api.main import app

    response = TestClient(app).get("/api/blast/jobs/job-repro-1/reproducibility")

    assert response.status_code == 200
    assert response.headers["x-elb-export-format"] == "reproducibility-v1"
    assert "job-repro-1-reproducibility.json" in response.headers["content-disposition"]
    assert response.json()["availability"]["result_manifest"] is True


def test_reproducibility_route_tolerates_manifest_failure(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Repo(_state()))
    monkeypatch.setattr(
        "api.services.job_artifacts.read_json_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("storage down")),
    )

    from api.main import app

    response = TestClient(app).get("/api/blast/jobs/job-repro-1/reproducibility")

    assert response.status_code == 200
    assert response.json()["result_manifest"] is None
    assert response.json()["availability"]["result_manifest"] is False


def test_reproducibility_route_enforces_owner_and_missing(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    from api.main import app

    client = TestClient(app)
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: _Repo(_state(owner_oid="another-user")),
    )
    assert client.get("/api/blast/jobs/job-repro-1/reproducibility").status_code == 403

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Repo(None))
    assert client.get("/api/blast/jobs/missing/reproducibility").status_code == 404
