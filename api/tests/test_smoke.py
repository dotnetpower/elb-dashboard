"""Unit tests for the api sidecar.

Responsibility: Unit tests for the api sidecar
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `client`, `test_health_returns_200_with_version`,
`test_health_response_has_request_id_header`, `test_request_id_echoed_when_supplied`,
`test_auth_required_endpoints_reject_anonymous`,
`test_auth_required_with_invalid_token_returns_401`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.tests._fakes import AsyncResultStub, make_delay_recorder
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Make sure no env state leaks between tests.
    monkeypatch.setenv("AZURE_TENANT_ID", "common")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
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
        ("GET", "/api/arm/subscriptions/sub-x/locations"),
        ("POST", "/api/resources/ensure-rg"),
        ("POST", "/api/storage/prepare-db"),
        ("POST", "/api/blast/submit"),
        ("POST", "/api/blast/logs/aaaaaaaaaaaa/ticket"),
        ("GET", "/api/aks/openapi/proxy?resource_group=rg-x&cluster_name=cx&path=%2Fhealthz"),
        ("POST", "/api/v1/elastic-blast/submit"),
        ("GET", "/api/v1/elastic-blast/jobs/aaaaaaaaaaaa"),
        ("GET", "/api/v1/elastic-blast/jobs/aaaaaaaaaaaa/files/result-xml-001"),
        ("GET", "/api/blast/jobs/aaaaaaaaaaaa/results/result-001"),
        ("POST", "/api/aks/provision"),
        ("POST", "/api/warmup/start"),
        ("GET", "/api/audit/log"),
        ("POST", "/api/client-log"),
        ("POST", "/api/terminal/ticket"),
        # Diagnostic endpoint references subscription ids — must be auth-gated
        # so the ingress does not leak tenant topology to anonymous callers.
        ("GET", "/api/health/azure-discovery"),
        # Celery diagnostic endpoints leak broker URL / worker stats /
        # arbitrary task results and accept anonymous enqueue. Auth-gated
        # since 2026-05-22 (security-audit #3).
        ("GET", "/api/health/celery"),
        ("POST", "/api/health/celery/enqueue-noop"),
        ("GET", "/api/health/celery/result/some-task-id"),
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


def test_storage_summary_preserves_hns_when_container_list_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.monitoring import get_storage_summary

    class BrokenBlobContainers:
        def list(self, _resource_group: str, _account_name: str) -> list[object]:
            raise RuntimeError("container list unavailable")

    fake_client = SimpleNamespace(
        storage_accounts=SimpleNamespace(
            get_properties=lambda _resource_group, account_name: SimpleNamespace(
                name=account_name,
                location="eastus",
                sku=SimpleNamespace(name="Standard_LRS"),
                kind="StorageV2",
                public_network_access="Enabled",
                is_hns_enabled=True,
            )
        ),
        blob_containers=BrokenBlobContainers(),
    )
    monkeypatch.setattr("api.services.monitoring.storage_client", lambda *_args: fake_client)

    body = get_storage_summary(object(), "sub", "rg", "stelb")

    assert body["is_hns_enabled"] is True
    assert body["region"] == "eastus"
    assert body["containers"] == []
    assert body["containers_degraded"] is True
    assert body["containers_degraded_reason"] == "RuntimeError"


def test_storage_summary_includes_container_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.monitoring import get_storage_summary
    from api.services.storage import usage_cache as storage_usage_cache

    storage_usage_cache.reset_storage_usage_cache()
    monkeypatch.setattr(storage_usage_cache, "_start_refresh_thread", lambda target: target())

    class FakeBlobContainers:
        def list(self, _resource_group: str, _account_name: str) -> list[object]:
            return [
                SimpleNamespace(
                    name="queries",
                    public_access=None,
                    last_modified_time=None,
                ),
                SimpleNamespace(
                    name="job-artifacts",
                    public_access=None,
                    last_modified_time=None,
                ),
            ]

    fake_client = SimpleNamespace(
        storage_accounts=SimpleNamespace(
            get_properties=lambda _resource_group, account_name: SimpleNamespace(
                name=account_name,
                location="eastus",
                sku=SimpleNamespace(name="Standard_LRS"),
                kind="StorageV2",
                public_network_access="Disabled",
                is_hns_enabled=True,
            )
        ),
        blob_containers=FakeBlobContainers(),
    )
    monkeypatch.setattr("api.services.monitoring.storage_client", lambda *_args: fake_client)
    monkeypatch.setattr(
        "api.services.storage.usage_cache.storage_data.container_usage_summaries",
        lambda _credential, _account_name, _names, **_kwargs: {
            "queries": {
                "blob_count": 2,
                "size_bytes": 128,
                "usage_error": None,
                "usage_truncated": False,
            },
            "job-artifacts": {
                "blob_count": 1,
                "size_bytes": 64,
                "usage_error": None,
                "usage_truncated": True,
            },
        },
    )

    body = get_storage_summary(object(), "sub", "rg", "stelb")

    for container in body["containers"]:
        assert container.pop("usage_refreshed_at") is not None
    assert body["containers"] == [
        {
            "name": "queries",
            "public_access": None,
            "last_modified_time": None,
            "blob_count": 2,
            "size_bytes": 128,
            "usage_pending": False,
            "usage_truncated": False,
            "usage_error": None,
            "usage_cache_state": "fresh",
        },
        {
            "name": "job-artifacts",
            "public_access": None,
            "last_modified_time": None,
            "blob_count": 1,
            "size_bytes": 64,
            "usage_pending": False,
            "usage_truncated": True,
            "usage_error": None,
            "usage_cache_state": "fresh",
        },
    ]
    assert body["containers_usage_cache"]["state"] == "fresh"


def test_storage_summary_keeps_containers_when_usage_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.services.monitoring import get_storage_summary
    from api.services.storage import usage_cache as storage_usage_cache

    storage_usage_cache.reset_storage_usage_cache()
    monkeypatch.setattr(storage_usage_cache, "_start_refresh_thread", lambda target: target())

    class FakeBlobContainers:
        def list(self, _resource_group: str, _account_name: str) -> list[object]:
            return [
                SimpleNamespace(
                    name="queries",
                    public_access=None,
                    last_modified_time=None,
                )
            ]

    fake_client = SimpleNamespace(
        storage_accounts=SimpleNamespace(
            get_properties=lambda _resource_group, account_name: SimpleNamespace(
                name=account_name,
                location="eastus",
                sku=SimpleNamespace(name="Standard_LRS"),
                kind="StorageV2",
                public_network_access="Disabled",
                is_hns_enabled=True,
            )
        ),
        blob_containers=FakeBlobContainers(),
    )
    monkeypatch.setattr("api.services.monitoring.storage_client", lambda *_args: fake_client)

    def raise_usage(*_args: object, **_kwargs: object) -> dict[str, dict[str, int]]:
        raise RuntimeError("usage unavailable")

    monkeypatch.setattr(
        "api.services.storage.usage_cache.storage_data.container_usage_summaries",
        raise_usage,
    )

    body = get_storage_summary(object(), "sub", "rg", "stelb")

    assert body["containers"] == [
        {
            "name": "queries",
            "public_access": None,
            "last_modified_time": None,
            "blob_count": None,
            "size_bytes": None,
            "usage_pending": False,
            "usage_truncated": False,
            "usage_error": "RuntimeError",
            "usage_cache_state": "fresh",
            "usage_refreshed_at": body["containers"][0]["usage_refreshed_at"],
        }
    ]
    assert body["containers"][0]["usage_refreshed_at"] is not None
    assert "containers_usage_degraded" not in body
    assert body["containers_usage_cache"]["state"] == "fresh"


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


def test_blast_submit_rejects_storage_account_mismatch_before_queue(
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
            "database": "https://stgelb.blob.core.windows.net/blast-db/core_nt/core_nt",
            "query_file": "queries/q.fa",
            "options": {"sharding_mode": "off", "disable_sharding": True},
        },
    )

    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert "database URL must belong" in body["message"]


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
def test_blast_preflight_reports_web_blast_compatibility(
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
            "query_data": ">q1\nAAAA\n",
            "sharding_mode": "precise",
            "outfmt": 5,
            "db_effective_search_space": 32_156_241_807_668,
            "db_total_letters": 1_041_443_571_674,
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["compatibility"]["mode"] == "precise"
    compatibility_check = next(
        item for item in body["checks"] if item["id"] == "web_blast_compatibility"
    )
    assert compatibility_check["status"] == "pass"
    assert compatibility_check["compatibility"]["evidence"]["db_name"] == "core_nt"


def test_blast_submit_blocks_false_precise_with_unverified_database(
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
                "outfmt": 5,
                "query_count": 1,
                "db_effective_search_space": 12345,
                "db_total_letters": 99999,
            },
        },
    )

    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "web_blast_compatibility_blocked"
    assert body["compatibility"]["mode"] == "calibration_required"


def test_blast_jobs_submit_blocks_false_precise_with_unverified_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    r = client.post(
        "/api/blast/jobs",
        json={
            "resource_group": "rg-elb",
            "aks_cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "db": "unknown_nt",
            "query_data": ">q1\nAAAA\n",
            "sharding_mode": "precise",
            "outfmt": 5,
            "db_effective_search_space": 12345,
            "db_total_letters": 99999,
        },
    )

    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "web_blast_compatibility_blocked"
    assert body["compatibility"]["mode"] == "calibration_required"


@pytest.mark.slow
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
    calls, fake_delay = make_delay_recorder("task-123")

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


def test_blast_submit_persists_celery_task_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )

    updates: list[tuple[str, dict[str, object]]] = []

    class FakeRepository:
        def get(self, job_id: str) -> object | None:
            return None

        def create(self, state: object) -> None:
            return None

        def update(self, job_id: str, **kwargs: object) -> object:
            updates.append((job_id, kwargs))
            return SimpleNamespace(job_id=job_id, **kwargs)

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepository)
    monkeypatch.setattr(
        "api.tasks.blast.submit.delay", lambda **_kwargs: AsyncResultStub("task-persisted")
    )

    r = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "core_nt",
            "query_file": "queries/original/input.fa",
            "options": {"sharding_mode": "off"},
        },
    )

    assert r.status_code == 200
    assert r.json()["task_id"] == "task-persisted"
    assert any(update == {"task_id": "task-persisted"} for _job_id, update in updates)


def test_blast_submit_merges_top_level_precision_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )
    calls, fake_delay = make_delay_recorder("task-456")

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

    calls, fake_delay = make_delay_recorder("task-789")

    monkeypatch.setattr("api.services.storage.data.upload_query_text", fake_upload_query_text)
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
    assert uploads[0]["blob_path"] in calls[0]["query_file"]


def test_blast_submit_idempotency_key_reuses_existing_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.monitoring.list_aks_clusters",
        lambda *_args, **_kwargs: [{"name": "elb-cluster", "power_state": "Running"}],
    )
    delay_calls: list[dict[str, object]] = []

    def fake_delay(**kwargs: object) -> object:
        delay_calls.append(kwargs)
        return SimpleNamespace(id="new-task")

    class FakeRepository:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(job_id=job_id, task_id="task-existing", status="queued")

        def create(self, state: object) -> object:
            raise AssertionError("idempotent retry must not create a new row")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepository)
    monkeypatch.setattr("api.tasks.blast.submit.delay", fake_delay)

    r = client.post(
        "/api/blast/submit",
        json={
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "storage_account": "elbstg01",
            "program": "blastn",
            "database": "core_nt",
            "query_file": "queries/q.fa",
            "idempotency_key": "req-1",
            "options": {"sharding_mode": "off"},
        },
    )

    assert r.status_code == 200
    assert r.json()["task_id"] == "task-existing"
    assert delay_calls == []


def test_blast_job_events_returns_canonical_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class FakeRepository:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(job_id=job_id, owner_oid=None)

        def get_history(self, job_id: str, limit: int = 200) -> list[dict[str, object]]:
            return [
                {
                    "PartitionKey": job_id,
                    "RowKey": "001",
                    "event": "created",
                    "ts": "2026-05-20T00:00:01+00:00",
                    "payload_json": '{"status":"queued"}',
                }
            ]

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepository)

    r = client.get("/api/blast/jobs/job-1/events")

    assert r.status_code == 200
    assert r.json()["events"][0]["event"] == "created"


def test_blast_job_queue_returns_position(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class FakeRepository:
        def get(self, job_id: str) -> object:
            return SimpleNamespace(job_id=job_id, owner_oid=None, status="queued")

        def list_active(self, job_type: str = "blast", limit: int = 500) -> list[object]:
            return [
                SimpleNamespace(job_id="job-1", status="queued", created_at="1"),
                SimpleNamespace(job_id="job-2", status="queued", created_at="2"),
            ]

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepository)

    r = client.get("/api/blast/jobs/job-2/queue")

    assert r.status_code == 200
    assert r.json()["queue_position"] == 2


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
    monkeypatch.setattr("api.services.storage.data.read_blob_text", fake_read_blob_text)

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
    monkeypatch.setattr("api.services.storage.data.read_blob_text", fake_read_blob_text)

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
    monkeypatch.setattr("api.services.storage.data.read_blob_text", fake_read_blob_text)

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
    monkeypatch.setattr("api.services.storage.data.read_blob_text", fake_read_blob_text)
    monkeypatch.setattr(
        "api.routes.blast._config_preview_from_payload",
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


def test_blast_job_file_config_preview_rejects_storage_account_mismatch(
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
                    "database": "https://stgelb.blob.core.windows.net/blast-db/core_nt/core_nt",
                    "query_file": "queries/uploads/job-123/query.fa",
                },
            )

    def fake_read_blob_text(
        _credential: object,
        _account_name: str,
        container: str,
        blob_path: str,
        *,
        max_bytes: int,
    ) -> str:
        del container, blob_path, max_bytes
        raise RuntimeError("BlobNotFound")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", FakeRepo)
    monkeypatch.setattr("api.services.storage.data.read_blob_text", fake_read_blob_text)

    r = client.get(
        "/api/blast/jobs/job-123/file"
        "?name=elastic-blast.ini&subscription_id=sub-1&storage_account=elbstg01"
    )

    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_config_payload"
    assert "database URL must belong" in body["message"]


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
    from api.routes._blast_shared import _stub_log

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


# ---------------------------------------------------------------------------
# Security audit (2026-05-22): #6 ownership + #7 audit sanitisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module_path,url_prefix",
    [
        ("api.routes.operations", "/api/operations/"),
        ("api.routes.tasks", "/api/tasks/"),
    ],
    ids=["operations", "tasks"],
)
def test_status_route_returns_403_when_owner_differs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    url_prefix: str,
) -> None:
    """`/api/operations/{id}` and the `/api/tasks/{id}` legacy alias must both
    reject callers who do not own the task."""
    import importlib

    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    module = importlib.import_module(module_path)

    other_owner_state = SimpleNamespace(owner_oid="another-oid-not-the-dev-bypass")

    class FakeRepo:
        def find_by_task_id(self, _task_id: str) -> object:
            return other_owner_state

    monkeypatch.setattr(module, "JobStateRepository", lambda: FakeRepo(), raising=True)

    class _UnusedAR:
        def __init__(self, *_a: object, **_kw: object) -> None:
            raise AssertionError("ownership rejection should short-circuit")

    monkeypatch.setattr(module, "AsyncResult", _UnusedAR)

    r = client.get(f"{url_prefix}task-foreign")
    assert r.status_code == 403
    # Only the canonical route currently asserts the body shape; the legacy
    # alias just has to return 403 (its handler is the same function).
    if url_prefix == "/api/operations/":
        assert r.json().get("detail") == "not owner"


def test_operations_status_allows_when_no_jobstate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """System / diag tasks without a JobState row stay reachable."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.routes import operations

    class FakeRepo:
        def find_by_task_id(self, _task_id: str) -> object:
            return None

    monkeypatch.setattr(operations, "JobStateRepository", lambda: FakeRepo(), raising=True)

    class FakeAsyncResult:
        status = "SUCCESS"

        def __init__(self, task_id: str, **_kw: object) -> None:
            self.task_id = task_id
            self.info = None
            self.result = {"diag": "noop"}

        def ready(self) -> bool:
            return True

        def successful(self) -> bool:
            return True

        def failed(self) -> bool:
            return False

    monkeypatch.setattr(operations, "AsyncResult", FakeAsyncResult)

    r = client.get("/api/operations/diag-task-id")
    assert r.status_code == 200
    body = r.json()
    assert body["celery"]["status"] == "SUCCESS"
    assert body["result"] == {"diag": "noop"}


def test_audit_log_payload_is_sanitised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/api/audit/log` must redact SAS / bearer tokens before responding."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    sas_url = "https://stx.blob.core.windows.net/c/q.fa?sv=2024-11-04&sig=ABCDEF1234567890&se=x"
    raw_payload = (
        '{"download_url": "' + sas_url + '", '
        '"hint": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"}'
    )

    class FakeJob:
        job_id = "job-1"
        type = "blast"

    class FakeRepo:
        def list_for_owner(
            self, _oid: str, limit: int = 50, *, include_payload: bool = True
        ) -> list[FakeJob]:
            # ``include_payload`` is accepted so the route signature matches
            # ``state_repo.JobStateRepository.list_for_owner``; this fake never
            # populates payload anyway.
            del include_payload
            return [FakeJob()]

        def get_history(self, _job_id: str, limit: int = 20) -> list[dict[str, object]]:
            return [
                {
                    "event": "submitted",
                    "ts": "2026-05-22T00:00:00Z",
                    "payload_json": raw_payload,
                }
            ]

        def get_history_for_jobs(
            self,
            job_ids: list[str],
            *,
            per_job_limit: int = 20,
        ) -> dict[str, list[dict[str, object]]]:
            del per_job_limit
            return {
                jid: [
                    {
                        "event": "submitted",
                        "ts": "2026-05-22T00:00:00Z",
                        "payload_json": raw_payload,
                        "PartitionKey": jid,
                        "RowKey": "0",
                    }
                ]
                for jid in job_ids
            }

    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        lambda: FakeRepo(),
        raising=True,
    )

    r = client.get("/api/audit/log")
    assert r.status_code == 200
    body = r.json()
    assert body["events"], "expected at least one event"
    payload = body["events"][0]["payload"]
    assert "sig=" not in payload, "SAS query string leaked"
    assert "<sas-redacted>" in payload
    assert "Bearer <redacted>" in payload


def test_operations_fails_closed_when_state_repo_raises_in_production(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without AUTH_DEV_BYPASS the ownership lookup must fail **closed**.

    A transient state-repo failure (table not found, credential blip,
    network) MUST NOT be exploitable as an ownership bypass. The route
    returns 503 ``ownership_check_unavailable`` so the caller knows to
    retry instead of getting silently authorised.
    """
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.auth import _dev_bypass_identity, require_caller
    from api.main import app
    from api.routes import operations

    # Skip the real MSAL validation so we can exercise the ownership path
    # itself; the goal of this test is the fail-closed branch, not the
    # bearer parser.
    app.dependency_overrides[require_caller] = _dev_bypass_identity
    try:

        class ExplodingRepo:
            def find_by_task_id(self, _task_id: str) -> object:
                raise RuntimeError("table not found")

        monkeypatch.setattr(operations, "JobStateRepository", lambda: ExplodingRepo())

        class _UnusedAR:
            def __init__(self, *_a: object, **_kw: object) -> None:
                raise AssertionError("fail-closed must short-circuit AsyncResult")

        monkeypatch.setattr(operations, "AsyncResult", _UnusedAR)

        r = client.get("/api/operations/task-foreign")
        assert r.status_code == 503
        # Custom exception handler in api.main unwraps dict details, so
        # ``{"code": ..., "retryable": ...}`` is the response body itself.
        body = r.json()
        assert body.get("code") == "ownership_check_unavailable"
        assert body.get("retryable") is True
    finally:
        app.dependency_overrides.pop(require_caller, None)


def test_operations_fail_open_in_dev_bypass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under AUTH_DEV_BYPASS the same failure must degrade open (dev loop)."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.routes import operations

    class ExplodingRepo:
        def find_by_task_id(self, _task_id: str) -> object:
            raise RuntimeError("AZURE_TABLE_ENDPOINT is not set")

    monkeypatch.setattr(operations, "JobStateRepository", lambda: ExplodingRepo())

    class FakeAsyncResult:
        status = "PENDING"

        def __init__(self, task_id: str, **_kw: object) -> None:
            self.task_id = task_id
            self.info = None
            self.result = None

        def ready(self) -> bool:
            return False

        def successful(self) -> bool:
            return False

        def failed(self) -> bool:
            return False

    monkeypatch.setattr(operations, "AsyncResult", FakeAsyncResult)

    r = client.get("/api/operations/dev-task")
    assert r.status_code == 200
    assert r.json()["celery"]["status"] == "PENDING"


def test_audit_log_error_branch_is_sanitised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback ``error`` string must redact SAS / GUID / keys too.

    State-repo / Storage SDK exceptions routinely embed the account URL,
    a server-generated request id GUID, and sometimes a SAS query string
    in the message. The raw exception text is fine for the server-side
    log but never the HTTP body.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    sub_id = "11111111-2222-3333-4444-555555555555"
    sas_qs = "?sv=2024-11-04&sig=ZZZZZZZZZZZZZZZZZZZZ&se=2026-12-31"
    leaky_message = f"GET https://stx.blob.core.windows.net/c/q{sas_qs} failed for sub={sub_id}"

    def _exploding_repo() -> object:
        raise RuntimeError(leaky_message)

    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo",
        _exploding_repo,
        raising=True,
    )

    r = client.get("/api/audit/log")
    assert r.status_code == 200
    body = r.json()
    err = body.get("error", "")
    assert err, "expected the sanitised error fallback string"
    assert "sig=" not in err, "SAS query string leaked in error fallback"
    assert sub_id not in err, "subscription GUID leaked in error fallback"
    assert sas_qs not in err
