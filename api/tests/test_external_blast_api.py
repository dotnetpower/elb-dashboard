from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


def test_external_blast_submit_forwards_contract(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured = {}

    def fake_submit(payload):
        captured.update(payload)
        return {
            "job_id": "aaaaaaaaaaaa",
            "status": "queued",
            "created_at": "2026-05-12T10:00:00Z",
            "blast_version": "2.17.0+",
            "db_name": "core_nt",
            "db_version": "2026-05-02",
        }

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    client = TestClient(app)

    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "taxid": 3431483,
            "is_inclusive": False,
            "options": {
                "outfmt": 5,
                "word_size": 28,
                "dust": True,
                "evalue": 0.05,
                "max_target_seqs": 500,
            },
            "batch_len": 462,
            "idempotency_key": "req-1",
        },
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == "aaaaaaaaaaaa"
    assert captured["submission_source"] == "external_api"
    assert "external_correlation_id" not in captured
    assert captured["taxid"] == 3431483
    assert captured["is_inclusive"] is False
    assert captured["options"]["outfmt"] == 5
    assert captured["batch_len"] == 462
    assert "caller_oid" not in captured


def test_external_blast_rejects_non_xml_outfmt(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "options": {"outfmt": 6}},
    )

    assert response.status_code == 422


def test_external_blast_rejects_invalid_program(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "program": "rm -rf"},
    )

    assert response.status_code == 422


def test_external_blast_rejects_string_priority(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/elastic-blast/submit",
        json={"query_fasta": ">q1\nATGC", "db": "core_nt", "priority": "urgent"},
    )

    assert response.status_code == 422


def test_upstream_error_detail_is_sanitised() -> None:
    from api.services import external_blast

    detail = external_blast._sanitise_detail(
        {"message": "failed ?sig=a&sp=b&se=c Bearer abcdefghijklmnopqrstuvwxyz1234567890"}
    )

    assert "sig=a" not in detail["message"]
    assert "Bearer abc" not in detail["message"]


def test_streaming_upstream_error_detail_is_read_and_sanitised() -> None:
    from api.services import external_blast

    request = httpx.Request("GET", "https://example.test/result")
    response = httpx.Response(
        500,
        request=request,
        stream=httpx.ByteStream(b'{"message":"failed ?sig=secret-token"}'),
    )
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(HTTPException) as raised:
        external_blast._raise_upstream_error(exc)

    assert raised.value.status_code == 500
    assert raised.value.detail["message"] == "failed ?sig=secret-token"


def test_upstream_filename_is_sanitised() -> None:
    from api.services import external_blast

    assert external_blast._safe_filename('"bad\r\nX-Test: 1.xml"') == "bad__X-Test__1.xml"


def test_external_blast_status_forwards(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "running",
            "progress_pct": 45,
            "created_at": "2026-05-12T10:00:00Z",
        },
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa")

    assert response.status_code == 200
    assert response.json()["progress_pct"] == 45


def test_external_blast_rejects_unsafe_job_id(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.get("/api/v1/elastic-blast/jobs/bad$id")

    assert response.status_code == 422


def test_external_blast_rejects_non_hex_job_id(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app

    client = TestClient(app)
    response = client.get("/api/v1/elastic-blast/jobs/ABC123")

    assert response.status_code == 422


def test_path_segment_is_percent_encoded() -> None:
    from api.services import external_blast

    assert external_blast._path_segment("bad/id") == "bad%2Fid"


def test_external_blast_file_download_forwards(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "stream_file",
        lambda job_id, file_id: external_blast.StreamedFile(
            chunks=iter([b"<Blast", b"Output />"]),
            media_type="application/xml",
            filename="blast_result.xml",
        ),
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/files/result-xml-001")

    assert response.status_code == 200
    assert response.content == b"<BlastOutput />"
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["content-disposition"] == 'attachment; filename="blast_result.xml"'


def test_canonical_jobs_list_merges_external_when_table_unconfigured(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "list_jobs",
        lambda: {
            "jobs": [
                {
                    "job_id": "aaaaaaaaaaaa",
                    "status": "running",
                    "created_at": "2026-05-12T10:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                    "cluster_name": "elb-cluster",
                }
            ],
            "count": 1,
        },
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"][0]["job_id"] == "aaaaaaaaaaaa"
    assert body["jobs"][0]["source"] == "external_api"
    assert body["jobs"][0]["infrastructure"] == {"cluster_name": "elb-cluster"}
    assert "degraded" not in body


def test_canonical_jobs_list_reports_external_detail_code(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_unavailable():
        raise HTTPException(
            503,
            detail={"code": "openapi_not_configured", "message": "ELB_OPENAPI_BASE_URL is not set"},
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_unavailable)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == []
    assert body["external_degraded"] is True
    assert body["external_degraded_reason"] == "openapi_not_configured"
    assert "degraded" not in body


def test_canonical_submit_delegates_dashboard_payload(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import stubs

    def fake_submit(body, caller):
        assert body == {"program": "blastn"}
        assert caller.object_id
        return {
            "id": "local-job-1",
            "job_id": "local-job-1",
            "instance_id": "task-1",
            "status": "queued",
        }

    monkeypatch.setattr(stubs, "blast_submit", fake_submit)
    client = TestClient(app)

    response = client.post("/api/blast/jobs", json={"program": "blastn"})

    assert response.status_code == 202
    assert response.json()["job_id"] == "local-job-1"
    assert response.json()["instance_id"] == "task-1"


def test_canonical_job_get_falls_back_to_external(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "success",
            "created_at": "2026-05-12T10:00:00Z",
            "completed_at": "2026-05-12T10:03:00Z",
            "db_name": "core_nt",
            "result": {"files": []},
        },
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs/aaaaaaaaaaaa")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["phase"] == "completed"
    assert body["db"] == "core_nt"


def test_canonical_results_list_falls_back_to_external_files(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {
            "job_id": job_id,
            "status": "success",
            "result": {
                "files": [
                    {
                        "file_id": "result-001",
                        "filename": "batch_001.xml",
                        "format": "blast_xml",
                        "size_bytes": 123,
                    }
                ]
            },
        },
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs/aaaaaaaaaaaa/results")

    assert response.status_code == 200
    body = response.json()
    assert body["files"] == body["results"]
    assert body["files"][0]["file_id"] == "result-001"
    assert body["files"][0]["name"] == "batch_001.xml"


def test_canonical_result_file_streams_external_file_id(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "stream_file",
        lambda job_id, file_id: external_blast.StreamedFile(
            chunks=iter([b"<Blast", b"Output />"]),
            media_type="application/xml",
            filename="batch_001.xml",
        ),
    )
    client = TestClient(app)

    response = client.get("/api/blast/jobs/aaaaaaaaaaaa/results/result-001")

    assert response.status_code == 200
    assert response.content == b"<BlastOutput />"
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["content-disposition"] == 'attachment; filename="batch_001.xml"'


def test_canonical_local_result_file_id_must_match_job_prefix(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.services.storage_data import encode_blob_file_id

    client = TestClient(app)
    other_job_file_id = encode_blob_file_id("bbbbbbbbbbbb/batch_001.xml")

    response = client.get(
        f"/api/blast/jobs/aaaaaaaaaaaa/results/{other_job_file_id}",
        params={"storage_account": "stexample"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file_id"


def test_local_blob_file_id_round_trips_and_rejects_traversal() -> None:
    from api.services.storage_data import decode_blob_file_id, encode_blob_file_id

    file_id = encode_blob_file_id("aaaaaaaaaaaa/batch_001.xml")

    assert decode_blob_file_id(file_id) == "aaaaaaaaaaaa/batch_001.xml"
    with pytest.raises(ValueError):
        decode_blob_file_id(encode_blob_file_id("../secret.xml"))
