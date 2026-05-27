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

