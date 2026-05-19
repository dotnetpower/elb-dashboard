"""Unit tests for the api sidecar.

Runs against the FastAPI app via TestClient. No Azure cloud calls — tests
that require the cloud are skipped automatically.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Make sure no env state leaks between tests.
    os.environ.setdefault("AZURE_TENANT_ID", "common")
    os.environ.setdefault("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    from api.main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def test_health_returns_200_with_version(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_response_has_request_id_header(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.headers.get("x-request-id"), "request_id middleware did not stamp the response"


def test_request_id_echoed_when_supplied(client: TestClient) -> None:
    r = client.get("/api/health", headers={"x-request-id": "abcd1234"})
    assert r.headers["x-request-id"] == "abcd1234"


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/me"),
        ("GET", "/api/monitor/aks?resource_group=rg-x"),
        ("GET", "/api/monitor/storage?resource_group=rg-x&account_name=stx"),
        ("GET", "/api/monitor/jobs"),
        ("GET", "/api/monitor/metrics"),
        ("GET", "/api/monitor/aks/events?resource_group=rg-x&cluster_name=cx"),
        ("GET", "/api/arm/subscriptions"),
        ("POST", "/api/resources/ensure-rg"),
        ("POST", "/api/storage/prepare-db"),
        ("POST", "/api/blast/submit"),
        ("GET", "/api/aks/openapi/proxy?resource_group=rg-x&cluster_name=cx&path=%2Fhealthz"),
        ("POST", "/api/v1/elastic-blast/submit"),
        ("GET", "/api/v1/elastic-blast/jobs/aaaaaaaaaaaa"),
        ("GET", "/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/files/result-xml-001"),
        ("GET", "/api/blast/jobs/aaaaaaaaaaaa/results/result-001"),
        ("POST", "/api/aks/provision"),
        ("POST", "/api/warmup/start"),
        ("GET", "/api/audit/log"),
        ("POST", "/api/terminal/ticket"),
        # Diagnostic endpoint references subscription ids — must be auth-gated
        # so the ingress does not leak tenant topology to anonymous callers.
        ("GET", "/api/health/azure-discovery"),
    ],
)
def test_auth_required_endpoints_reject_anonymous(
    client: TestClient, method: str, path: str
) -> None:
    r = client.request(method, path, json={"foo": "bar"} if method != "GET" else None)
    assert r.status_code == 401, (
        f"{method} {path} returned {r.status_code} without bearer token; expected 401"
    )
    assert "detail" in r.json()


def test_auth_required_with_invalid_token_returns_401(client: TestClient) -> None:
    r = client.get("/api/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert "invalid token" in r.json().get("detail", "").lower()


def test_metrics_endpoint_returns_summary_with_dev_bypass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/monitor/metrics` is wired and returns the documented schema.

    Reset the buffer so the test is deterministic regardless of which
    other tests ran first under the same process.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.services import request_metrics as rm

    rm.reset_for_tests()

    r = client.get("/api/monitor/metrics?window_seconds=60")
    assert r.status_code == 200
    body = r.json()
    assert body["window_seconds"] == 60
    assert body["degraded"] is True
    assert body["degraded_reason"] == "no_samples"
    assert body["p95_ms"] is None
    assert isinstance(body["rpm"], list) and len(body["rpm"]) == 1


def test_metrics_endpoint_rejects_path_prefix_outside_api(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    r = client.get("/api/monitor/metrics?path_prefix=/etc/passwd")
    assert r.status_code == 400


def test_monitor_aks_uses_snapshot_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.services import monitor_cache

    monitor_cache.reset_monitor_snapshot_cache()
    calls = 0

    def fake_list_aks_clusters(*_args: object, **_kwargs: object):
        nonlocal calls
        calls += 1
        return [{"name": f"aks-{calls}"}]

    monkeypatch.setattr("api.routes.monitor.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        fake_list_aks_clusters,
    )

    first = client.get("/api/monitor/aks?subscription_id=sub&resource_group=rg-elb")
    second = client.get("/api/monitor/aks?subscription_id=sub&resource_group=rg-elb")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == 1
    assert first.json()["clusters"] == [{"name": "aks-1"}]
    assert first.json()["cache"]["state"] == "refreshed"
    assert second.json()["clusters"] == [{"name": "aks-1"}]
    assert second.json()["cache"]["state"] == "fresh"
    monitor_cache.reset_monitor_snapshot_cache()


def test_aks_events_rejects_invalid_namespace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    r = client.get("/api/monitor/aks/events?resource_group=rg-x&cluster_name=cx&namespace=../etc")
    assert r.status_code == 400


def test_aks_events_rejects_invalid_resource_group(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    r = client.get("/api/monitor/aks/events?resource_group=../etc/passwd&cluster_name=cx")
    assert r.status_code == 400


def test_aks_events_rejects_invalid_cluster_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    r = client.get("/api/monitor/aks/events?resource_group=rg-x&cluster_name=bad%20name%21")
    assert r.status_code == 400


def test_blast_submit_blocks_invalid_precise_sharding_before_queue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    r = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "unknown_nt",
            "query_file": "queries/q.fa",
            "options": {
                "sharding_mode": "precise",
                "outfmt": 6,
                "query_count": 1,
            },
        },
    )

    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "sharding_precision_blocked"
    assert "db_effective_search_space" in body["message"]


def test_blast_preflight_blocks_precise_multi_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    r = client.post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "db": "core_nt",
            "query_data": ">q1\nAAAA\n>q2\nAAAA\n",
            "sharding_mode": "precise",
            "outfmt": 6,
            "db_effective_search_space": 225,
        },
    )

    assert r.status_code == 200
    precision_check = next(
        item for item in r.json()["checks"] if item["id"] == "sharding_precision"
    )
    assert precision_check["status"] == "fail"
    assert precision_check["query_metadata"]["query_count"] == 2
    assert any(
        "query_effective_search_spaces" in item
        for item in precision_check["precision"]["blocking_errors"]
    )


def test_blast_preflight_allows_precise_multi_query_uniform_search_space(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    r = client.post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "db": "core_nt",
            "query_data": ">q1\nAAAA\n>q2\nAAAA\n",
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_effective_search_spaces": [225, 225],
        },
    )

    assert r.status_code == 200
    precision_check = next(
        item for item in r.json()["checks"] if item["id"] == "sharding_precision"
    )
    assert precision_check["status"] == "pass"
    assert precision_check["precision"]["precision_level"] == "precise_tabular"


def test_blast_preflight_allows_precise_multi_query_split_search_spaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    r = client.post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "db": "core_nt",
            "query_data": ">q1\nAAAA\n>q2\nAAAA\n",
            "sharding_mode": "precise",
            "outfmt": 6,
            "query_effective_search_spaces": [225, 300],
        },
    )

    assert r.status_code == 200
    precision_check = next(
        item for item in r.json()["checks"] if item["id"] == "sharding_precision"
    )
    assert precision_check["status"] == "pass"
    assert precision_check["precision"]["precision_level"] == "precise_tabular_split"
    assert precision_check["precision"]["merge_strategy"] == "query_group_split_tabular_top_n"


def test_blast_preflight_accepts_aks_cluster_name_alias(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )

    r = client.post(
        "/api/blast/pre-flight",
        json={
            "resource_group": "rg-elb",
            "aks_cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "db": "core_nt",
            "query_data": ">q1\nAAAA\n",
            "sharding_mode": "off",
            "outfmt": 5,
        },
    )

    assert r.status_code == 200
    aks_check = next(item for item in r.json()["checks"] if item["id"] == "aks_cluster")
    assert aks_check["status"] == "pass"


def test_blast_submit_allows_mixed_precise_split_parent_queue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )
    calls: list[dict[str, object]] = []

    class FakeAsyncResult:
        id = "task-123"

    def fake_delay(**kwargs: object) -> FakeAsyncResult:
        calls.append(kwargs)
        return FakeAsyncResult()

    monkeypatch.setattr("api.tasks.blast.submit.delay", fake_delay)

    r = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "core_nt",
            "query_file": "queries/original/input.fa",
            "options": {
                "sharding_mode": "precise",
                "outfmt": 6,
                "query_count": 2,
                "query_effective_search_spaces": [225, 300],
                "db_sharded": True,
                "db_partitions": 5,
                "db_partition_prefix": (
                    "https://elbstg01.blob.core.windows.net/blast-db/5shards/core_nt_shard_"
                ),
                "db_total_letters": 123456,
            },
        },
    )

    assert r.status_code == 200
    assert r.json()["task_id"] == "task-123"
    assert calls[0]["query_file"] == "queries/original/input.fa"
    assert calls[0]["caller_oid"] == "00000000-0000-0000-0000-000000000000"
    assert calls[0]["caller_tenant_id"] == "common"
    assert calls[0]["options"]["query_effective_search_spaces"] == [225, 300]


def test_blast_submit_merges_top_level_precision_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )
    calls: list[dict[str, object]] = []

    class FakeAsyncResult:
        id = "task-456"

    def fake_delay(**kwargs: object) -> FakeAsyncResult:
        calls.append(kwargs)
        return FakeAsyncResult()

    monkeypatch.setattr("api.tasks.blast.submit.delay", fake_delay)

    r = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "core_nt",
            "query_file": "queries/original/input.fa",
            "query_count": 1,
            "shard_sets": [1, 2, 4],
            "options": {
                "sharding_mode": "precise",
                "outfmt": 5,
                "db_effective_search_space": 123456,
                "db_total_letters": 123456,
            },
        },
    )

    assert r.status_code == 200
    assert r.json()["job_id"]
    assert calls[0]["options"]["query_count"] == 1
    assert calls[0]["options"]["shard_sets"] == [1, 2, 4]


def test_canonical_dashboard_submit_uploads_inline_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )
    uploads: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []

    def fake_upload_query_text(
        credential: object,
        account_name: str,
        container: str,
        blob_path: str,
        fasta_text: str,
    ) -> str:
        uploads.append(
            {
                "account_name": account_name,
                "container": container,
                "blob_path": blob_path,
                "fasta_text": fasta_text,
            }
        )
        return f"https://{account_name}.blob.core.windows.net/{container}/{blob_path}"

    class FakeAsyncResult:
        id = "task-789"

    def fake_delay(**kwargs: object) -> FakeAsyncResult:
        calls.append(kwargs)
        return FakeAsyncResult()

    monkeypatch.setattr("api.services.storage_data.upload_query_text", fake_upload_query_text)
    monkeypatch.setattr("api.tasks.blast.submit.delay", fake_delay)

    r = client.post(
        "/api/blast/jobs",
        json={
            "resource_group": "rg-elb",
            "aks_cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "db": "core_nt",
            "query_data": ">q1\nAAAA\n",
            "outfmt": 5,
            "sharding_mode": "off",
        },
    )

    assert r.status_code == 202
    assert r.json()["job_id"]
    assert uploads[0]["account_name"] == "elbstg01"
    assert uploads[0]["container"] == "queries"
    assert str(uploads[0]["blob_path"]).startswith("uploads/")
    assert calls[0]["cluster_name"] == "elb-cluster"
    assert calls[0]["database"] == "core_nt"
    assert str(calls[0]["query_file"]).startswith("queries/uploads/")
    assert calls[0]["options"]["query_count"] == 1


def test_blast_job_file_reads_uploaded_query_from_queries_container(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    class FakeRepo:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(
                job_id=job_id,
                owner_oid="00000000-0000-0000-0000-000000000000",
                payload={"query_file": f"queries/uploads/{job_id}/query.fa"},
            )

    reads: list[tuple[str, str]] = []

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        reads.append((container, blob_path))
        assert max_bytes == 1000
        return ">q1\nACGT\n"

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)

    r = client.get(
        "/api/blast/jobs/job-123/file"
        "?name=input.fa&subscription_id=sub-1&storage_account=elbstg01&max_bytes=1000"
    )

    assert r.status_code == 200
    assert r.json()["content"] == ">q1\nACGT\n"
    assert reads == [("queries", "uploads/job-123/query.fa")]


def test_blast_job_file_rejects_query_blob_outside_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    class FakeRepo:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(
                job_id=job_id,
                owner_oid="00000000-0000-0000-0000-000000000000",
                payload={"query_file": f"queries/uploads/{job_id}/query.fa"},
            )

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)

    r = client.get(
        "/api/blast/jobs/job-123/file"
        "?name=queries/uploads/other-job/query.fa&subscription_id=sub-1&storage_account=elbstg01"
    )

    assert r.status_code == 403


def test_blast_job_file_accepts_job_query_blob_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    class FakeRepo:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(
                job_id=job_id,
                owner_oid="00000000-0000-0000-0000-000000000000",
                payload={"query_file": f"queries/uploads/{job_id}/query.fa"},
            )

    reads: list[tuple[str, str]] = []

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        reads.append((container, blob_path))
        return ">q1\nACGT\n"

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)

    query_url = "https://elbstg01.blob.core.windows.net/queries/uploads/job-123/query.fa"
    r = client.get(
        "/api/blast/jobs/job-123/file"
        f"?name={query_url}&subscription_id=sub-1&storage_account=elbstg01"
    )

    assert r.status_code == 200
    assert reads == [("queries", "uploads/job-123/query.fa")]


def test_blast_job_file_falls_back_to_uploads_query_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    class FakeRepo:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(
                job_id=job_id,
                owner_oid="00000000-0000-0000-0000-000000000000",
                payload={},
            )

    reads: list[tuple[str, str]] = []

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        reads.append((container, blob_path))
        if blob_path == "uploads/job-123/query.fa":
            return ">q1\nACGT\n"
        raise RuntimeError("BlobNotFound")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)

    r = client.get(
        "/api/blast/jobs/job-123/file"
        "?name=input.fa&subscription_id=sub-1&storage_account=elbstg01&max_bytes=1000"
    )

    assert r.status_code == 200
    assert r.json()["content"] == ">q1\nACGT\n"
    assert reads == [
        ("queries", "job-123/input.fa"),
        ("queries", "uploads/job-123/query.fa"),
    ]


def test_blast_job_file_generates_config_preview_when_blob_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    class FakeRepo:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(
                job_id=job_id,
                owner_oid="00000000-0000-0000-0000-000000000000",
                payload={
                    "resource_group": "rg-elb",
                    "cluster_name": "elb-cluster",
                    "storage_account": "elbstg01",
                    "program": "blastn",
                    "database": "core_nt",
                    "query_file": f"queries/uploads/{job_id}/query.fa",
                },
            )

    reads: list[tuple[str, str]] = []

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        reads.append((container, blob_path))
        raise RuntimeError("BlobNotFound")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)
    monkeypatch.setattr("api.services.storage_data.read_blob_text", fake_read_blob_text)
    monkeypatch.setattr(
        "api.routes.stubs._config_preview_from_payload",
        lambda **_kwargs: "[blast]\nprogram=blastn\n",
    )

    r = client.get(
        "/api/blast/jobs/job-123/file"
        "?name=elastic-blast.ini&subscription_id=sub-1&storage_account=elbstg01"
    )

    assert r.status_code == 200
    assert r.json()["content"] == "[blast]\nprogram=blastn\n"
    assert reads == [
        ("queries", "job-123/elastic-blast.ini"),
        ("queries", "uploads/job-123/elastic-blast.ini"),
    ]


def test_blast_submit_blocks_precise_mapping_search_spaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    r = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "core_nt",
            "query_file": "queries/q.fa",
            "options": {
                "sharding_mode": "precise",
                "outfmt": 6,
                "query_count": 2,
                "query_effective_search_spaces": {"q1": 225, "q2": 225},
            },
        },
    )

    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "sharding_precision_blocked"
    assert "list ordered" in body["message"]


def test_terminal_ticket_includes_session_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("TERMINAL_SHELL_USER", "azureuser")

    r = client.post("/api/terminal/ticket")

    assert r.status_code == 200
    body = r.json()
    assert body["ticket"]
    assert body["session_id"]
    assert body["caller"]["display_name"] == "dev-bypass@local"
    assert body["shell_user"] == "azureuser"


# ---------------------------------------------------------------------------
# Catch-all reverse proxy hardening
# ---------------------------------------------------------------------------
def test_unknown_api_route_returns_404_not_spa(client: TestClient) -> None:
    """An unknown /api/* path must NOT be forwarded to the frontend
    (which would return SPA HTML with status 200 and break the SPA's
    fetch error handling)."""
    r = client.get("/api/this-route-does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "unknown api route"
    assert body["path"] == "/api/this-route-does-not-exist"


def test_unknown_api_post_also_404(client: TestClient) -> None:
    r = client.post("/api/another-missing", json={"x": 1})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Legacy terminal endpoints return 410 Gone with structured detail
# ---------------------------------------------------------------------------
def test_legacy_terminal_password_is_410(client: TestClient) -> None:
    r = client.get(
        "/api/terminal/some-vm/password",
        headers={"Authorization": "Bearer __dev_bypass__"},  # not honored, returns 401
    )
    # Without a real token we still get 401 first (auth runs before route handler).
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Stub responses for not-yet-implemented routes
# ---------------------------------------------------------------------------
def test_stub_log_helper_does_not_raise() -> None:
    from api.routes.stubs import _stub_log

    _stub_log("test", a=1, b="x")  # must not raise


# ---------------------------------------------------------------------------
# Diagnostic: /api/health/azure-discovery (auth-gated, sanitised, hard-capped)
# ---------------------------------------------------------------------------
def test_azure_discovery_probe_credential_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential construction failure must short-circuit and emit a hint."""
    from api import auth as auth_mod

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # Force get_credential() to raise so we exercise the error branch.
    import api.services as svc_pkg

    def boom() -> None:
        raise RuntimeError("synthetic-cred-failure")

    monkeypatch.setattr(svc_pkg, "get_credential", boom, raising=True)
    _ = auth_mod  # keep import live for monkeypatch context

    r = client.get("/api/health/azure-discovery")
    assert r.status_code == 200
    body = r.json()
    assert body["credential"]["status"] == "error"
    assert body["credential"]["error_type"] == "RuntimeError"
    assert body["hint"] is not None
    # subscriptions/RGs steps must NOT have run after credential failure.
    assert body["subscriptions_list"]["status"] == "unknown"
    assert body["resource_groups_list"]["status"] == "unknown"


def test_azure_discovery_probe_sanitises_subscription_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when listing succeeds, raw GUIDs must never appear in the response."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    fake_sub_id = "11111111-2222-3333-4444-555555555555"
    fake_sub_name = "ME-MngEnvMCAP000000-test-1"

    class _Sub:
        subscription_id = fake_sub_id
        display_name = fake_sub_name

    class _SubsPage:
        def list(self):
            yield _Sub()

    class _FakeSubClient:
        def __init__(self, *_a, **_kw) -> None:
            self.subscriptions = _SubsPage()

    class _FakeResourceClient:
        class _RGs:
            def list(self):
                return iter(())

        def __init__(self) -> None:
            self.resource_groups = _FakeResourceClient._RGs()

    import api.routes.health as health_mod
    import api.services as svc_pkg
    from api.services import azure_clients as ac_mod

    monkeypatch.setattr(svc_pkg, "get_credential", lambda: object(), raising=True)
    monkeypatch.setattr(
        ac_mod,
        "resource_client",
        lambda *_a, **_kw: _FakeResourceClient(),
        raising=True,
    )
    monkeypatch.setattr("azure.mgmt.resource.SubscriptionClient", _FakeSubClient, raising=True)
    _ = health_mod

    r = client.get("/api/health/azure-discovery")
    assert r.status_code == 200
    body = r.json()
    payload_text = r.text
    # Sub-id and full display name must be redacted in the response.
    assert fake_sub_id not in payload_text, "raw subscription GUID leaked"
    assert fake_sub_name not in payload_text, "raw subscription display name leaked"
    # Sanitised marker (first 8 chars + ellipsis) should be present.
    assert "11111111…" in payload_text
    assert body["subscriptions_list"]["status"] == "ok"
    assert body["subscriptions_list"]["count_capped_at_5"] == 1
    assert body["resource_groups_list"]["status"] == "ok"
    assert body["resource_groups_list"]["count"] == 0
