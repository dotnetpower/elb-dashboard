"""Tests for External BLAST API behavior.

Responsibility: Tests for External BLAST API behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_external_blast_submit_forwards_contract`,
`test_canonical_jobs_external_submit_uses_trusted_metadata`,
`test_external_blast_events_falls_back_to_current_status`,
`test_external_blast_manifest_maps_result_files`, `test_external_blast_rejects_non_xml_outfmt`,
`test_external_blast_rejects_invalid_program`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_external_blast_api.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

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
            "submission_source": "dashboard",
            "external_correlation_id": "caller-supplied",
        },
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == "aaaaaaaaaaaa"
    assert captured["submission_source"] == "external_api"
    assert captured["external_correlation_id"] != "caller-supplied"
    assert captured["idempotency_key"] == "req-1"
    assert captured["canonical_request"]["metadata"]["submission_source"] == "external_api"
    assert captured["compatibility_contract"]["mode"] == "precise"
    assert captured["provenance"]["compatibility"]["mode"] == "precise"
    assert captured["taxid"] == 3431483
    assert captured["is_inclusive"] is False
    assert captured["options"]["outfmt"] == 5
    assert captured["batch_len"] == 462
    assert "caller_oid" not in captured


def test_canonical_jobs_external_submit_uses_trusted_metadata(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    captured = {}

    def fake_submit(payload):
        captured.update(payload)
        return {"job_id": "aaaaaaaaaaaa", "status": "queued"}

    monkeypatch.setattr(external_blast, "submit_job", fake_submit)
    client = TestClient(app)

    response = client.post(
        "/api/blast/jobs",
        json={
            "query_fasta": ">q1\nATGCATGCATGC",
            "db": "core_nt",
            "program": "blastn",
            "idempotency_key": "req-2",
            "submission_source": "dashboard",
        },
    )

    assert response.status_code == 202
    assert captured["submission_source"] == "external_api"
    assert captured["idempotency_key"] == "req-2"
    assert captured["external_correlation_id"]
    assert captured["canonical_request"]["query"]["kind"] == "inline_fasta"
    assert captured["compatibility_contract"]["mode"] == "precise"
    assert captured["provenance"]["query"]["sha256"]


def test_external_blast_events_falls_back_to_current_status(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda job_id: {"job_id": job_id, "status": "running", "updated_at": "now"},
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/events")

    assert response.status_code == 200
    assert response.json()["events"][0]["phase"] == "running"


def test_external_blast_manifest_maps_result_files(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast

    monkeypatch.setattr(
        external_blast,
        "get_job",
        lambda _job_id: {
            "result": {
                "files": [
                    {
                        "file_id": "result-xml",
                        "filename": "blast_result.xml",
                        "format": "blast_xml",
                        "size_bytes": 42,
                    }
                ]
            }
        },
    )
    client = TestClient(app)

    response = client.get("/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/manifest")

    assert response.status_code == 200
    assert response.json()["parseable_count"] == 1


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


def test_upstream_error_logs_sanitised_response_detail(caplog: pytest.LogCaptureFixture) -> None:
    from api.services import external_blast

    caplog.set_level("WARNING", logger="api.services.external_blast")
    request = httpx.Request("GET", "https://example.test/api/v1/elastic-blast/jobs/job-1")
    response = httpx.Response(
        400,
        request=request,
        json={
            "message": "bad request Bearer abcdefghijklmnopqrstuvwxyz1234567890",
        },
    )
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(HTTPException):
        external_blast._raise_upstream_error(exc)

    text = caplog.text
    assert "OpenAPI upstream request failed" in text
    assert "status=400" in text
    assert "https://example.test/api/v1/elastic-blast/jobs/job-1" in text
    assert "bad request" in text
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "Bearer <redacted>" in text


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


def test_external_blast_base_url_uses_runtime_cache(monkeypatch) -> None:
    monkeypatch.delenv("ELB_OPENAPI_BASE_URL", raising=False)
    from api.services import external_blast, openapi_runtime

    monkeypatch.setattr(openapi_runtime, "get_openapi_base_url", lambda: "http://10.0.0.4")

    assert external_blast._base_url() == "http://10.0.0.4"


def test_external_blast_headers_include_api_and_internal_tokens(monkeypatch) -> None:
    from api.services import external_blast

    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "api-token")
    monkeypatch.setenv("ELB_OPENAPI_INTERNAL_TOKEN", "internal-token")

    assert external_blast._headers() == {
        "Accept": "application/json",
        "X-ELB-API-Token": "api-token",
        "X-ELB-Internal-Token": "internal-token",
    }


def test_openapi_runtime_round_trip() -> None:
    from api.services import openapi_runtime

    class FakeRedis:
        def __init__(self) -> None:
            self.value: str | None = None

        def set(self, _key: str, value: str) -> None:
            self.value = value

        def get(self, _key: str) -> str | None:
            return self.value

    fake = FakeRedis()
    assert openapi_runtime.save_openapi_base_url(
        "http://10.0.0.4/",
        metadata={"cluster_name": "elb-cluster"},
        client=fake,
    )
    assert openapi_runtime.get_openapi_base_url(client=fake) == "http://10.0.0.4"


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


def test_canonical_jobs_list_uses_cluster_openapi_context(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    from api.main import app
    from api.routes import blast as stubs
    from api.services import external_blast

    captured: dict[str, str] = {}

    monkeypatch.setattr(
        stubs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}.{resource_group}.{subscription_id}",
            "api_token": "cluster-token",
        },
    )

    def list_jobs(**kwargs):
        captured.update(kwargs)
        return {
            "jobs": [
                {
                    "job_id": "bbbbbbbbbbbb",
                    "status": "success",
                    "created_at": "2026-05-12T10:00:00Z",
                    "program": "blastn",
                    "db": "core_nt",
                }
            ],
            "count": 1,
        }

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    client = TestClient(app)

    response = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    assert captured == {
        "base_url": "http://elb-cluster.rg-elb-01.sub-1",
        "api_token": "cluster-token",
    }
    body = response.json()
    assert body["jobs"][0]["job_id"] == "bbbbbbbbbbbb"
    assert body["jobs"][0]["status"] == "completed"


def test_canonical_jobs_list_filters_local_rows_by_scope(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class ScopedRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return [
                SimpleNamespace(
                    job_id="matching-job",
                    task_id=None,
                    type="blast",
                    status="completed",
                    phase="completed",
                    created_at="2026-05-19T10:00:00Z",
                    updated_at="2026-05-19T10:01:00Z",
                    error_code=None,
                    parent_job_id=None,
                    payload={
                        "subscription_id": "sub-1",
                        "resource_group": "rg-elb-01",
                        "cluster_name": "elb-cluster",
                        "program": "blastn",
                        "db": "core_nt",
                    },
                ),
                SimpleNamespace(
                    job_id="other-cluster-job",
                    task_id=None,
                    type="blast",
                    status="completed",
                    phase="completed",
                    created_at="2026-05-19T11:00:00Z",
                    updated_at="2026-05-19T11:01:00Z",
                    error_code=None,
                    parent_job_id=None,
                    payload={
                        "subscription_id": "sub-1",
                        "resource_group": "rg-elb-01",
                        "cluster_name": "other-cluster",
                        "program": "blastn",
                        "db": "core_nt",
                    },
                ),
            ]

        def list_children_for_owner(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(state_repo, "JobStateRepository", ScopedRepo)
    monkeypatch.setattr(external_blast, "list_jobs", lambda **_kwargs: {"jobs": []})
    client = TestClient(app)

    response = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    assert [job["job_id"] for job in response.json()["jobs"]] == ["matching-job"]


def test_canonical_jobs_list_enriches_external_rows_with_detail(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import blast as stubs
    from api.services import external_blast, state_repo

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    captured: dict[str, dict[str, str]] = {}

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(
        stubs,
        "_openapi_client_kwargs_from_cluster",
        lambda subscription_id, resource_group, cluster_name: {
            "base_url": f"http://{cluster_name}.{resource_group}.{subscription_id}",
            "api_token": "cluster-token",
        },
    )

    def list_jobs(**kwargs):
        captured["list"] = kwargs
        return {
            "jobs": [
                {
                    "job_id": "cccccccccccc",
                    "status": "running",
                    "created_at": "2026-05-19T10:42:09Z",
                    "program": "blastn",
                    "db": "https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt",
                    "cluster_name": "elb-cluster",
                }
            ],
            "count": 1,
        }

    def get_job(job_id: str, **kwargs):
        captured["get"] = {"job_id": job_id, **kwargs}
        return {
            "job_id": job_id,
            "status": "success",
            "created_at": "2026-05-19T10:42:09Z",
            "completed_at": "2026-05-19T10:44:14Z",
            "program": "blastn",
            "db_name": "16S_ribosomal_RNA",
            "query_file": "queries/uploads/probe/query.fa",
            "cluster_name": "elb-cluster",
            "execution": {
                "shard_count": 1,
                "shards_succeeded": 1,
                "shards_active": 0,
                "shards_failed": 0,
            },
            "result": {"files": [{"name": "batch_000.out.gz"}]},
        }

    monkeypatch.setattr(external_blast, "list_jobs", list_jobs)
    monkeypatch.setattr(external_blast, "get_job", get_job)
    client = TestClient(app)

    response = client.get(
        "/api/blast/jobs?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    assert captured["list"] == {
        "base_url": "http://elb-cluster.rg-elb-01.sub-1",
        "api_token": "cluster-token",
    }
    assert captured["get"] == {
        "job_id": "cccccccccccc",
        "base_url": "http://elb-cluster.rg-elb-01.sub-1",
        "api_token": "cluster-token",
    }
    row = response.json()["jobs"][0]
    assert row["status"] == "completed"
    assert row["phase"] == "completed"
    assert row["db"] == "16S_ribosomal_RNA"
    assert row["query_label"] == "query.fa"
    assert row["splits_total"] == 1
    assert row["splits_done"] == 1
    assert row["output"]["execution"]["shards_succeeded"] == 1


@pytest.mark.parametrize("external_status", ["success", "completed"])
def test_external_completed_status_maps_to_dashboard_completed(external_status):
    from api.routes import _blast_shared as stubs

    assert stubs._external_status_to_dashboard(external_status) == "completed"


def test_sync_external_jobs_creates_missing_rows(monkeypatch):
    """External rows that the Table does not yet know about MUST be
    persisted on first sight so they survive an AKS restart."""
    from api.routes import _blast_shared as shared

    created: list[object] = []
    updated: list[dict[str, object]] = []

    class FakeRepo:
        def get_many(self, ids):
            return {}

        def create(self, state):
            created.append(state)
            return state

        def update(self, *_a, **_kw):
            updated.append({"args": _a, "kw": _kw})

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.job_id = kwargs.get("job_id")
            self.status = kwargs.get("status")
            self.phase = kwargs.get("phase")

    monkeypatch.setattr(
        shared,
        "_sync_external_jobs_to_table",
        shared._sync_external_jobs_to_table,
    )

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "running",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
                "cluster_name": "elb-cluster",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (1, 0, set())
    assert len(created) == 1
    assert created[0].kwargs["job_id"] == "abc123"
    assert created[0].kwargs["status"] == "running"
    assert updated == []


def test_sync_external_jobs_updates_drifted_status(monkeypatch):
    """When the external plane reports a different status than the Table,
    the Table row MUST be refreshed so the list view is not stale."""
    from api.routes import _blast_shared as shared

    updated_calls: list[dict[str, object]] = []
    created: list[object] = []

    class ExistingRow:
        status = "running"
        phase = "running"

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": ExistingRow()}

        def update(self, job_id, **kwargs):
            updated_calls.append({"job_id": job_id, **kwargs})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    class FakeJobState:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())
    monkeypatch.setattr(state_repo, "JobState", FakeJobState)

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "success",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 1, set())
    assert created == []
    assert updated_calls == [
        {"job_id": "abc123", "status": "completed", "phase": "completed"}
    ]


def test_sync_external_jobs_skips_unchanged_status(monkeypatch):
    """If the status has not drifted, the sync MUST NOT call update.

    This avoids appending a new jobhistory row on every poll cycle."""
    from api.routes import _blast_shared as shared

    updated_calls: list[object] = []
    created: list[object] = []

    class ExistingRow:
        status = "completed"
        phase = "completed"

    class FakeRepo:
        def get_many(self, ids):
            return {"abc123": ExistingRow()}

        def update(self, *_a, **_kw):
            updated_calls.append({"args": _a, "kw": _kw})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "abc123",
                "status": "success",
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="00000000-0000-0000-0000-000000000000",
    )

    assert result == (0, 0, set())


def test_external_jobs_cache_serves_repeat_requests(monkeypatch):
    """Two back-to-back calls within the TTL MUST hit the upstream only once."""
    from api.routes import _blast_shared as shared

    hits = {"count": 0}

    class FakeExternal:
        @staticmethod
        def list_jobs(**_kwargs):
            hits["count"] += 1
            return {"jobs": [{"job_id": "x1"}], "count": 1}

    from api.services import external_blast

    monkeypatch.setattr(external_blast, "list_jobs", FakeExternal.list_jobs)
    # Cache fixture in conftest already cleared it; do nothing extra.

    rows1 = shared._external_list_jobs_cached({"base_url": "http://cluster"})
    rows2 = shared._external_list_jobs_cached({"base_url": "http://cluster"})

    assert rows1 == rows2 == [{"job_id": "x1"}]
    assert hits["count"] == 1


def test_canonical_jobs_list_reports_external_detail_code(monkeypatch):
    """Runtime failures from the external plane (5xx, network, timeout) MUST
    still surface as ``external_degraded`` so operators can see real outages
    in the request inspector. Configuration-absence reasons
    (``openapi_not_configured``) are exercised by
    :func:`test_canonical_jobs_list_silent_when_external_not_configured`.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_unavailable():
        raise HTTPException(
            502,
            detail={"code": "openapi_upstream_error", "message": "bad gateway"},
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_unavailable)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == []
    assert body["external_degraded"] is True
    assert body["external_degraded_reason"] == "openapi_upstream_error"
    assert "degraded" not in body


def test_canonical_jobs_list_silent_when_external_not_configured(monkeypatch):
    """``openapi_not_configured`` / ``openapi_not_enabled`` mean the optional
    external execution plane simply isn't wired up — that's a normal state,
    not a degradation. The Jobs payload MUST NOT carry ``external_degraded``
    in that case, otherwise the request inspector renders every 30 s poll as
    a red Degraded badge and operators tune out the real warnings.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.services import external_blast, state_repo

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_not_configured():
        raise HTTPException(
            503,
            detail={
                "code": "openapi_not_configured",
                "message": "ELB_OPENAPI_BASE_URL is not set",
            },
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_not_configured)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == []
    assert "external_degraded" not in body
    assert "external_degraded_reason" not in body
    assert "degraded" not in body


@pytest.mark.parametrize(
    "reason_code",
    ["openapi_not_configured", "openapi_not_enabled"],
)
def test_canonical_jobs_list_silent_for_every_not_enabled_reason(monkeypatch, reason_code):
    """Lock the entire ``_EXTERNAL_NOT_ENABLED_REASONS`` allow-list. Adding a
    new code to that set without an accompanying test would let a regression
    slip through.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import _blast_shared as stubs_module
    from api.services import external_blast, state_repo

    assert reason_code in stubs_module._EXTERNAL_NOT_ENABLED_REASONS

    class EmptyRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return []

    def list_jobs_not_enabled():
        raise HTTPException(
            503,
            detail={"code": reason_code, "message": f"{reason_code} active"},
        )

    monkeypatch.setattr(state_repo, "JobStateRepository", EmptyRepo)
    monkeypatch.setattr(external_blast, "list_jobs", list_jobs_not_enabled)
    client = TestClient(app)

    response = client.get("/api/blast/jobs")

    assert response.status_code == 200
    body = response.json()
    assert "external_degraded" not in body
    assert "external_degraded_reason" not in body
    assert "degraded" not in body


def test_canonical_submit_delegates_dashboard_payload(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import blast as stubs

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


def test_submit_marks_row_broker_unavailable_when_celery_down(monkeypatch):
    """If the broker is unreachable, the row MUST flip to failed/broker_unavailable
    instead of sitting on ``queued`` forever — otherwise the dashboard counts
    a zombie row as an active job."""
    from fastapi import HTTPException as _HTTPException

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes import blast as stubs

    created: list[object] = []
    updates: list[tuple[str, dict[str, object]]] = []

    class FakeRepo:
        def create(self, state):
            created.append(state)
            return state

        def update(self, job_id, **kwargs):
            updates.append((job_id, kwargs))

    class FakeRepoFactory:
        def __call__(self):
            return FakeRepo()

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    def broker_down(*_args, **_kwargs):
        raise _HTTPException(
            status_code=503,
            detail={"code": "broker_unavailable", "retryable": True},
        )

    monkeypatch.setattr(stubs, "_safe_delay", broker_down)

    client = TestClient(app)
    response = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "blast-db/core_nt/core_nt",
            "query_file": "queries/q.fa",
        },
    )

    assert response.status_code == 503
    assert len(created) == 1
    assert updates, "broker failure must trigger row cleanup"
    assert updates[0][1]["status"] == "failed"
    assert updates[0][1]["phase"] == "broker_unavailable"
    assert updates[0][1]["error_code"] == "broker_unavailable"


def test_sync_skips_tombstoned_deleted_rows(monkeypatch):
    """A row tombstoned by ``DELETE /api/blast/jobs/{id}`` (status=='deleted')
    MUST NOT be resurrected by a subsequent external-OpenAPI sync."""
    from api.routes import _blast_shared as shared

    class TombstoneRow:
        status = "deleted"
        phase = "deleted"

    updates: list[object] = []
    created: list[object] = []

    class FakeRepo:
        def get_many(self, _ids):
            return {"zzz999": TombstoneRow()}

        def update(self, *_a, **_kw):
            updates.append({"a": _a, "kw": _kw})

        def create(self, state):
            created.append(state)
            return state

    fake_repo = FakeRepo()

    class FakeRepoFactory:
        def __call__(self):
            return fake_repo

    from api.services import state_repo

    monkeypatch.setattr(state_repo, "JobStateRepository", FakeRepoFactory())

    result = shared._sync_external_jobs_to_table(
        [
            {
                "job_id": "zzz999",
                "status": "completed",  # upstream still thinks the job exists
                "created_at": "2026-05-20T00:00:00Z",
                "program": "blastn",
                "db": "core_nt",
            }
        ],
        caller_oid="",
    )

    assert result == (0, 0, {"zzz999"})


def test_canonical_jobs_list_refreshes_active_local_rows(monkeypatch):
    """List endpoint must refresh active rows so the dashboard doesn't wait
    up to 60 s for the next beat reconcile to flip a finished job."""

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.main import app
    from api.routes.blast import jobs as jobs_route
    from api.services import external_blast, state_repo

    active_row = SimpleNamespace(
        job_id="active-row",
        task_id=None,
        type="blast",
        status="running",
        phase="running",
        created_at="2026-05-21T00:00:00Z",
        updated_at="2026-05-21T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        subscription_id="sub-1",
        resource_group="rg-elb-01",
        cluster_name="elb-cluster",
        storage_account="stelb",
        payload={},
    )
    terminal_row = SimpleNamespace(
        job_id="terminal-row",
        task_id=None,
        type="blast",
        status="completed",
        phase="completed",
        created_at="2026-05-21T00:00:00Z",
        updated_at="2026-05-21T00:01:30Z",
        error_code=None,
        parent_job_id=None,
        subscription_id="sub-1",
        resource_group="rg-elb-01",
        cluster_name="elb-cluster",
        storage_account="stelb",
        payload={},
    )

    class ListRepo:
        def list_for_owner(self, *_args, **_kwargs):
            return [active_row, terminal_row]

        def list_children_for_owner(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(state_repo, "JobStateRepository", ListRepo)
    monkeypatch.setattr(external_blast, "list_jobs", lambda **_kwargs: {"jobs": []})

    refreshed_ids: list[str] = []

    def fake_refresh(repo, state):
        refreshed_ids.append(state.job_id)
        return state

    monkeypatch.setattr(jobs_route, "_refresh_running_blast_state", fake_refresh)

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs"
        "?subscription_id=sub-1&resource_group=rg-elb-01&cluster_name=elb-cluster"
    )

    assert response.status_code == 200
    # Only the active row's phase ∈ _K8S_REFRESH_PHASES → refresh called once.
    assert refreshed_ids == ["active-row"]
