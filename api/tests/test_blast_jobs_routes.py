"""HTTP-level tests for BLAST job detail routes.

Responsibility: HTTP-level tests for BLAST job list/detail response shaping
Edit boundaries: Keep tests focused on route behavior; use fakes instead of live Azure calls.
Key entry points: `test_job_detail_skips_split_child_lookup_for_non_split_job`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_jobs_routes.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient


def test_job_detail_skips_split_child_lookup_for_non_split_job(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    owner_oid = "00000000-0000-0000-0000-000000000000"

    class Repo:
        def get(self, job_id: str):
            assert job_id == "job-1"
            return SimpleNamespace(
                job_id="job-1",
                task_id="task-1",
                type="blast",
                owner_oid=owner_oid,
                status="completed",
                phase="completed",
                created_at="2026-05-21T00:00:00Z",
                updated_at="2026-05-21T00:01:00Z",
                error_code=None,
                parent_job_id=None,
                payload={"db": "core_nt", "query_file": "query.fa"},
            )

        def list_children(self, *_args, **_kwargs):
            raise AssertionError("non-split job detail should not query child rows")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)

    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job-1",
        params={"include_database_metadata": "false"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-1"
    assert body["status"] == "completed"
    assert "split_children" not in body
    assert "database_metadata" not in body


def test_jobs_list_swr_serves_stale_and_revalidates(monkeypatch) -> None:
    """The jobs list is served stale-while-revalidate: fresh → cache hit,
    stale → immediate stale payload + one background rebuild, then the rebuilt
    payload once it lands."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    import api.services.blast.jobs_list_cache as cache_mod
    from api.routes.blast import jobs as jobs_mod

    cache_mod.reset_jobs_list_cache()

    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])

    calls = {"n": 0}

    def fake_compute(**_kwargs):
        calls["n"] += 1
        return {"jobs": [{"job_id": f"build-{calls['n']}"}], "meta": {}}

    monkeypatch.setattr(jobs_mod, "_compute_blast_jobs_response", fake_compute)

    from api.main import app

    client = TestClient(app)

    # Cold → synchronous build #1, cached.
    r1 = client.get("/api/blast/jobs")
    assert r1.status_code == 200
    assert r1.json()["jobs"][0]["job_id"] == "build-1"
    assert calls["n"] == 1

    # Within the fresh window → cache hit, no rebuild.
    r2 = client.get("/api/blast/jobs")
    assert r2.json()["jobs"][0]["job_id"] == "build-1"
    assert calls["n"] == 1

    # Into the stale window → stale payload served immediately AND a background
    # rebuild (#2) runs (TestClient executes background tasks after the response).
    clock["now"] += cache_mod.JOBS_LIST_CACHE_TTL_SECONDS + 0.01
    r3 = client.get("/api/blast/jobs")
    assert r3.json()["jobs"][0]["job_id"] == "build-1"  # stale served, not blocked
    assert calls["n"] == 2  # background revalidate ran

    # The rebuilt payload is now fresh and served on the next poll.
    r4 = client.get("/api/blast/jobs")
    assert r4.json()["jobs"][0]["job_id"] == "build-2"
    assert calls["n"] == 2



def _query_route_repo(payload: dict, *, storage_account: str = "elbstg01"):
    """Build a fake repo whose ``get`` returns a single owned state row."""
    owner_oid = "00000000-0000-0000-0000-000000000000"
    state = SimpleNamespace(
        job_id="job-q",
        task_id="task-q",
        type="blast",
        owner_oid=owner_oid,
        status="completed",
        phase="completed",
        created_at="2026-05-27T00:00:00Z",
        updated_at="2026-05-27T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        payload=payload,
        storage_account=storage_account,
    )

    class Repo:
        def get(self, job_id: str):
            assert job_id == "job-q"
            return state

    return Repo


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _FakeBlobClient:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def download_blob(self, *, offset: int = 0, length: int | None = None):
        end = len(self._data) if length is None else min(len(self._data), offset + length)
        return _FakeStream(self._data[offset:end])


class _FakeBlobService:
    def __init__(self, expected_container: str, blob: _FakeBlobClient) -> None:
        self._expected_container = expected_container
        self._blob = blob

    def get_blob_client(self, container: str, blob_path: str):
        assert container == self._expected_container, container
        assert blob_path, blob_path
        return self._blob


def test_blast_job_query_returns_original_fasta(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    fasta = ">seq1\nACGTACGTACGT\n"
    blob = _FakeBlobClient(fasta.encode("utf-8"))
    service = _FakeBlobService("queries", blob)
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        _query_route_repo({"query_file": "uploads/job-q/query.fa"}),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda credential, account_name: service,
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-q/query")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-q"
    assert body["query_text"] == fasta
    assert body["size_bytes"] == len(fasta.encode("utf-8"))
    assert body["max_bytes"] == 5 * 1024 * 1024


def test_blast_job_query_404_when_query_file_missing(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        _query_route_repo({"db": "core_nt"}),
    )

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-q/query")

    assert response.status_code == 404
    assert response.json()["code"] == "query_not_persisted"


def test_blast_job_query_413_when_blob_exceeds_cap(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # Pretend the blob is larger than the 5 MiB cap by returning more bytes
    # than ``read_metadata_blob_bytes`` accepts (it raises ValueError).
    oversized = b"A" * (5 * 1024 * 1024 + 16)
    blob = _FakeBlobClient(oversized)
    service = _FakeBlobService("queries", blob)
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        _query_route_repo({"query_file": "uploads/job-q/query.fa"}),
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda credential, account_name: service,
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-q/query")

    assert response.status_code == 413
    detail = response.json()
    assert detail["code"] == "query_too_large_for_edit"
    assert detail["max_bytes"] == 5 * 1024 * 1024


def test_blast_job_query_rejects_path_traversal(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # A corrupted JobState row points at "../leaked.fa" — the defensive
    # _validate_blob_path guard must reject it before reaching the SDK.
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        _query_route_repo({"query_file": "uploads/../leaked.fa"}),
    )

    def _should_not_call(*_args, **_kwargs):
        raise AssertionError("Storage SDK must not be invoked on a traversal path")

    monkeypatch.setattr(
        "api.services.storage.data._blob_service", _should_not_call
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-q/query")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_query_path"


def test_blast_job_query_reconstructs_external_blob(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # External (OpenAPI) jobs project their record under ``payload.external``
    # with no top-level query_file and an empty storage_account on the row.
    # The route must reconstruct ``queries/<openapi_job_id>.fa`` and recover
    # the storage account from the trusted db URL so Edit search can rehydrate
    # the original query the same way dashboard jobs do.
    fasta = ">extseq\nTTTTGGGGCCCCAAAA\n"
    captured: dict[str, str] = {}

    class _CapturingBlobService:
        def get_blob_client(self, container: str, blob_path: str):
            captured["container"] = container
            captured["blob_path"] = blob_path
            return _FakeBlobClient(fasta.encode("utf-8"))

    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        _query_route_repo(
            {
                "external": {
                    "job_id": "job-q",
                    "db": "https://elbstg01.blob.core.windows.net/blast-db/core_nt",
                }
            },
            storage_account="",
        ),
    )
    monkeypatch.setattr(
        "api.services.blast.db_metadata.extract_trusted_storage_account",
        lambda database: "elbstg01" if "elbstg01" in database else "",
    )
    monkeypatch.setattr(
        "api.services.storage.data._blob_service",
        lambda credential, account_name: _CapturingBlobService(),
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-q/query")

    assert response.status_code == 200
    assert response.json()["query_text"] == fasta
    assert captured == {"container": "queries", "blob_path": "job-q.fa"}


def test_blast_job_query_external_404_when_storage_account_untrusted(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    # An external job whose db URL points at a foreign (untrusted) account must
    # not leak the MI Storage token: the trusted-account gate returns "" and
    # the route degrades to 404 instead of reaching the Storage SDK.
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository",
        _query_route_repo(
            {
                "external": {
                    "job_id": "job-q",
                    "db": "https://attacker.blob.core.windows.net/blast-db/core_nt",
                }
            },
            storage_account="",
        ),
    )
    monkeypatch.setattr(
        "api.services.blast.db_metadata.extract_trusted_storage_account",
        lambda database: "elbstg01" if "elbstg01" in database else "",
    )

    def _should_not_call(*_args, **_kwargs):
        raise AssertionError("Storage SDK must not be invoked without a trusted account")

    monkeypatch.setattr("api.services.storage.data._blob_service", _should_not_call)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-q/query")

    assert response.status_code == 404
    assert response.json()["code"] == "query_not_persisted"


_DEV_BYPASS_OID = "00000000-0000-0000-0000-000000000000"


def _cancel_repo(state: SimpleNamespace):
    """Fake repo whose ``get`` returns ``state`` and records ``update`` calls."""

    class Repo:
        updates: ClassVar[list[dict]] = []

        def get(self, job_id: str):
            assert job_id == state.job_id
            return state

        def update(self, job_id: str, **kwargs):
            Repo.updates.append({"job_id": job_id, **kwargs})

    return Repo


def test_blast_job_cancel_external_routes_to_sibling_delete(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    state = SimpleNamespace(
        job_id="abc123",
        task_id="task-x",
        type="blast",
        owner_oid="",
        owner_upn="api",
        status="running",
        phase="running",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        payload={"external": {"job_id": "abc123"}},
        subscription_id="",
        resource_group="",
        cluster_name="",
        storage_account="",
    )
    repo_cls = _cancel_repo(state)
    repo_cls.updates = []
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", repo_cls)

    deleted: dict[str, str] = {}

    def fake_delete_job(job_id: str, **kwargs):
        deleted["job_id"] = job_id
        deleted.update({k: str(v) for k, v in kwargs.items()})
        return {"job_id": job_id, "status": "deleted"}

    monkeypatch.setattr("api.services.external_blast.delete_job", fake_delete_job)
    monkeypatch.setattr(
        "api.routes.blast._openapi_client_kwargs_from_cluster",
        lambda *_a, **_k: {},
    )

    def fail_safe_delay(*_a, **_k):
        raise AssertionError("external cancel must not enqueue the k8s cancel task")

    monkeypatch.setattr("api.routes.blast._safe_delay", fail_safe_delay)

    from api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/blast/jobs/abc123/cancel",
        json={"cluster_name": "elb-cluster", "resource_group": "rg-elb-dashboard"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["openapi_job_id"] == "abc123"
    assert deleted["job_id"] == "abc123"
    assert repo_cls.updates == [
        {"job_id": "abc123", "status": "cancelled", "phase": "cancelled"}
    ]


def test_blast_job_cancel_dashboard_uses_k8s_task(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    state = SimpleNamespace(
        job_id="dash-1",
        task_id="task-y",
        type="blast",
        owner_oid=_DEV_BYPASS_OID,
        owner_upn="user@example.com",
        status="running",
        phase="running",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        payload={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": "elb-cluster-02",
            "storage_account": "stelb01",
        },
    )
    repo_cls = _cancel_repo(state)
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", repo_cls)

    captured: dict[str, object] = {}

    class AsyncResultStub:
        id = "task-cancel-1"

    def fake_safe_delay(_task, **kwargs):
        captured.update(kwargs)
        return AsyncResultStub()

    monkeypatch.setattr("api.routes.blast._safe_delay", fake_safe_delay)

    def fail_delete_job(*_a, **_k):
        raise AssertionError("dashboard cancel must not call the sibling DELETE")

    monkeypatch.setattr("api.services.external_blast.delete_job", fail_delete_job)

    from api.main import app

    client = TestClient(app)
    response = client.post("/api/blast/jobs/dash-1/cancel", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelling"
    assert body["task_id"] == "task-cancel-1"
    # The route back-fills the scope from the stored payload.
    assert captured["cluster_name"] == "elb-cluster-02"
    assert captured["resource_group"] == "rg-elb-cluster"


def test_blast_job_cancel_external_sibling_unreachable_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    state = SimpleNamespace(
        job_id="abc123",
        task_id="task-x",
        type="blast",
        owner_oid="",
        owner_upn="api",
        status="running",
        phase="running",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        payload={"external": {"job_id": "abc123"}},
    )
    repo_cls = _cancel_repo(state)
    monkeypatch.setattr("api.services.state_repo.JobStateRepository", repo_cls)

    from fastapi import HTTPException

    def unreachable(*_a, **_k):
        raise HTTPException(
            503, detail={"code": "openapi_unreachable", "message": "down"}
        )

    monkeypatch.setattr("api.services.external_blast.delete_job", unreachable)
    monkeypatch.setattr(
        "api.routes.blast._openapi_client_kwargs_from_cluster",
        lambda *_a, **_k: {},
    )

    from api.main import app

    client = TestClient(app)
    response = client.post("/api/blast/jobs/abc123/cancel", json={})

    # The sibling's HTTPException is surfaced verbatim, not masked as a wrong
    # "cancel_unavailable" k8s failure.
    assert response.status_code == 503
    assert response.json()["code"] == "openapi_unreachable"


def _other_owner_state(job_id: str = "job-other") -> SimpleNamespace:
    """A job row owned by a different identity than the dev-bypass caller."""
    return SimpleNamespace(
        job_id=job_id,
        task_id="task-other",
        type="blast",
        owner_oid="11111111-1111-1111-1111-111111111111",
        status="completed",
        phase="completed",
        created_at="2026-06-03T00:00:00Z",
        updated_at="2026-06-03T00:01:00Z",
        error_code=None,
        parent_job_id=None,
        payload={"db": "core_nt", "query_file": "query.fa"},
    )


def test_assert_job_owner_isolation_default(monkeypatch) -> None:
    monkeypatch.delenv("BLAST_JOBS_SHARED_VISIBILITY", raising=False)

    from api.services.blast.job_state import (
        _assert_job_owner,
        blast_shared_visibility_enabled,
    )
    from fastapi import HTTPException

    assert blast_shared_visibility_enabled() is False
    caller = SimpleNamespace(object_id=_DEV_BYPASS_OID)

    # A foreign owner is rejected when the dev flag is off.
    with pytest.raises(HTTPException) as excinfo:
        _assert_job_owner("11111111-1111-1111-1111-111111111111", caller)
    assert excinfo.value.status_code == 403

    # An empty owner_oid (external / cluster-shared row) is always allowed.
    _assert_job_owner("", caller)
    # The caller's own job is allowed.
    _assert_job_owner(_DEV_BYPASS_OID, caller)


def test_assert_job_owner_relaxed_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("BLAST_JOBS_SHARED_VISIBILITY", "true")

    from api.services.blast.job_state import (
        _assert_job_owner,
        blast_shared_visibility_enabled,
    )

    assert blast_shared_visibility_enabled() is True
    caller = SimpleNamespace(object_id=_DEV_BYPASS_OID)
    # No exception even though the owner differs.
    _assert_job_owner("11111111-1111-1111-1111-111111111111", caller)


def test_job_detail_blocks_other_owner_when_flag_off(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("BLAST_JOBS_SHARED_VISIBILITY", raising=False)

    state = _other_owner_state()

    class Repo:
        def get(self, job_id: str):
            assert job_id == "job-other"
            return state

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)

    from api.main import app

    client = TestClient(app)
    response = client.get("/api/blast/jobs/job-other")

    assert response.status_code == 403
    assert response.json()["detail"] == "not owner"


def test_job_detail_allows_other_owner_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("BLAST_JOBS_SHARED_VISIBILITY", "true")

    state = _other_owner_state()

    class Repo:
        def get(self, job_id: str):
            assert job_id == "job-other"
            return state

        def list_children(self, *_args, **_kwargs):
            raise AssertionError("non-split job detail should not query child rows")

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)

    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job-other",
        params={"include_database_metadata": "false"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-other"
    assert body["status"] == "completed"

