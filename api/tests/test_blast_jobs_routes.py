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


def test_job_detail_recovers_query_label_for_external_job(monkeypatch) -> None:
    """An external job whose ephemeral Redis defline label was evicted recovers
    the Query ID durably from the query blob on the detail view."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    owner_oid = "00000000-0000-0000-0000-000000000000"

    class Repo:
        def get(self, job_id: str):
            return SimpleNamespace(
                job_id="job-ext",
                task_id="task-1",
                type="blast",
                owner_oid=owner_oid,
                status="completed",
                phase="completed",
                created_at="2026-06-19T00:00:00Z",
                updated_at="2026-06-19T00:01:00Z",
                error_code=None,
                parent_job_id=None,
                storage_account="elbstg01",
                db="https://elbstg01.blob.core.windows.net/blast-db/core_nt",
                # External (OpenAPI) job: no top-level query_file; the sibling
                # uploaded the inline FASTA to queries/<openapi_id>.fa.
                payload={"db": "core_nt", "external": {"job_id": "openapi-xyz"}},
            )

    reads: list[tuple[str, str]] = []

    def fake_read_blob_text(_cred, _account, container, blob_path, *, max_bytes) -> str:
        reads.append((container, blob_path))
        return ">myquery some description\nACGTACGT\n"

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)
    monkeypatch.setattr("api.services.storage.data.read_blob_text", fake_read_blob_text)

    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job-ext",
        params={"include_database_metadata": "false"},
    )

    assert response.status_code == 200
    body = response.json()
    # The durable query blob (queries/openapi-xyz.fa) was read and its first
    # defline derived into the Query ID.
    assert reads == [("queries", "openapi-xyz.fa")]
    assert body["query_label"] == "myquery"


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


def test_job_detail_recovers_external_failed_error_and_persists(monkeypatch) -> None:
    """An external-origin failed row with no error_code (failed before
    sync-time recovery shipped, or a submit-time failure with no Storage
    FAILURE.txt) recovers the real sibling cause on the detail render, persists
    it to error_code, and surfaces it in the banner — not the generic
    'no error detail' placeholder."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    owner_oid = "00000000-0000-0000-0000-000000000000"
    real_error = (
        "BLAST database core_nt memory requirements exceed memory available "
        'on selected machine type "Standard_E16s_v5"'
    )
    persisted: dict[str, object] = {}

    state = SimpleNamespace(
        job_id="ext-fail",
        task_id=None,
        type="blast",
        owner_oid=owner_oid,
        status="failed",
        phase="failed",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:01:00Z",
        error_code="",
        parent_job_id=None,
        subscription_id="sub-1",
        resource_group="rg-1",
        cluster_name="elb-cluster-01",
        storage_account="",
        payload={"external": {"job_id": "ext-fail", "status": "failed"}},
    )

    class Repo:
        def get(self, job_id: str):
            return state

        def update(self, job_id: str, **kwargs):
            persisted.update(kwargs)
            if "error_code" in kwargs:
                state.error_code = kwargs["error_code"]
            return state

        def list_children(self, *_a, **_k):
            return []

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)

    # The detail render resolves the sibling endpoint from the row's scope and
    # fetches the per-job detail (the LIST snapshot carries no ``error``).
    from api.services import external_blast

    calls: list[str] = []

    def fake_get_job(job_id, **_kwargs):
        calls.append(job_id)
        return {
            "job_id": job_id,
            "status": "failed",
            "error": {"code": "BLAST_FAILED", "message": real_error},
        }

    monkeypatch.setattr(external_blast, "get_job", fake_get_job)

    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/ext-fail",
        params={"include_database_metadata": "false"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert real_error in (body.get("error") or "")
    assert "no error detail" not in (body.get("error") or "").lower()
    # Persisted to the indexed column so subsequent renders skip the fetch.
    assert real_error in str(persisted.get("error_code") or "")
    assert calls == ["ext-fail"]


def test_jobs_list_swr_serves_stale_and_revalidates(monkeypatch) -> None:
    """The jobs list is served stale-while-revalidate. On a COLD cache the route
    serves a FAST local-only build immediately (``skip_enrichment``) and rebuilds
    the enriched payload in the background; thereafter fresh → cache hit, stale →
    immediate stale payload + one background rebuild, then the rebuilt payload."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    import api.services.blast.jobs_list_cache as cache_mod
    from api.routes.blast import jobs as jobs_mod

    cache_mod.reset_jobs_list_cache()

    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_mod.time, "monotonic", lambda: clock["now"])

    calls = {"n": 0}

    def fake_compute(**kwargs):
        calls["n"] += 1
        kind = "fast" if kwargs.get("skip_enrichment") else "full"
        return {"jobs": [{"job_id": f"{kind}-{calls['n']}"}], "meta": {}}

    monkeypatch.setattr(jobs_mod, "_compute_blast_jobs_response", fake_compute)

    from api.main import app

    client = TestClient(app)

    # Cold → FAST build #1 served immediately, then a background full rebuild #2
    # caches the enriched payload (TestClient runs background tasks after the
    # response).
    r1 = client.get("/api/blast/jobs")
    assert r1.status_code == 200
    assert r1.json()["jobs"][0]["job_id"] == "fast-1"  # fast paint, not blocked
    assert calls["n"] == 2  # fast build + background full rebuild

    # Within the fresh window → cache hit on the enriched payload, no rebuild.
    r2 = client.get("/api/blast/jobs")
    assert r2.json()["jobs"][0]["job_id"] == "full-2"
    assert calls["n"] == 2

    # Into the stale window → stale payload served immediately AND a background
    # full rebuild (#3) runs.
    clock["now"] += cache_mod.JOBS_LIST_CACHE_TTL_SECONDS + 0.01
    r3 = client.get("/api/blast/jobs")
    assert r3.json()["jobs"][0]["job_id"] == "full-2"  # stale served, not blocked
    assert calls["n"] == 3  # background revalidate ran

    # The rebuilt payload is now fresh and served on the next poll.
    r4 = client.get("/api/blast/jobs")
    assert r4.json()["jobs"][0]["job_id"] == "full-3"
    assert calls["n"] == 3


def test_jobs_list_cold_empty_local_falls_through_to_full_build(monkeypatch) -> None:
    """When the fast local-only build yields no rows the route must NOT serve an
    empty list — it falls through to the full synchronous build so external /
    sibling-only jobs are not hidden for a poll cycle."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    import api.services.blast.jobs_list_cache as cache_mod
    from api.routes.blast import jobs as jobs_mod

    cache_mod.reset_jobs_list_cache()

    calls: list[bool] = []

    def fake_compute(**kwargs):
        skip = bool(kwargs.get("skip_enrichment"))
        calls.append(skip)
        if skip:
            return {"jobs": [], "meta": {}}  # no local rows on the fast path
        return {"jobs": [{"job_id": "external-only"}], "meta": {}}

    monkeypatch.setattr(jobs_mod, "_compute_blast_jobs_response", fake_compute)

    from api.main import app

    client = TestClient(app)
    r = client.get("/api/blast/jobs")
    assert r.status_code == 200
    assert r.json()["jobs"][0]["job_id"] == "external-only"
    # Fast (skip) build attempted first, then the full build because it was empty.
    assert calls == [True, False]


def _pagination_route_setup(monkeypatch, *, row_count: int):
    """Wire a fake state repo + isolated compute helpers for the jobs list route.

    Returns ``(client, seen)`` where ``seen`` records the ``limit`` the repo's
    ``list_for_owner`` was called with, so a test can assert the fetch-one-extra
    probe (the route requests ``limit + 1`` to compute ``has_more`` honestly).
    Only the pagination plumbing is exercised; row→JSON mapping and external
    sync are stubbed so the test stays focused on slice + ``has_more`` logic.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    import api.services.blast.jobs_list_cache as cache_mod
    import api.services.state_repo as state_repo_mod
    from api.routes.blast import jobs as jobs_mod

    cache_mod.reset_jobs_list_cache()

    rows = [
        SimpleNamespace(
            job_id=f"job-{i}",
            type="blast",
            status="completed",
            phase="completed",
            owner_oid="00000000-0000-0000-0000-000000000000",
            created_at=f"2026-06-{(row_count - i):02d}T00:00:00Z",
        )
        for i in range(row_count)
    ]
    seen: dict[str, int] = {}

    class Repo:
        def list_for_owner(self, owner_oid, limit=50, *, include_payload=True):
            seen["limit"] = limit
            # Mimic the real repo: return at most ``limit`` genuinely-newest rows.
            return rows[:limit]

    monkeypatch.setattr(state_repo_mod, "get_state_repo", lambda: Repo())
    monkeypatch.setattr(
        jobs_mod,
        "_local_to_blast_job",
        lambda row, **_kwargs: {
            "job_id": row.job_id,
            "status": row.status,
            "created_at": row.created_at,
        },
    )
    monkeypatch.setattr(jobs_mod, "_local_state_matches_job_scope", lambda *a, **k: True)
    monkeypatch.setattr(
        jobs_mod, "_local_list_row_may_have_split_children", lambda _row: False
    )
    monkeypatch.setattr(jobs_mod, "_blocked_refresh_reasons", lambda _rows: {})
    monkeypatch.setattr(
        jobs_mod,
        "collect_and_sync_external_jobs",
        lambda **_kwargs: SimpleNamespace(
            rows=[], tombstoned_ids=set(), any_target_ok=True, target_failures=[]
        ),
    )

    from api.main import app

    return TestClient(app), seen


def test_jobs_list_page_envelope_has_more_when_more_rows_exist(monkeypatch) -> None:
    """A full page reports ``has_more=True`` and the route over-fetches by one
    (``limit + 1``) so the flag is honest without a server-side ordered index."""
    client, seen = _pagination_route_setup(monkeypatch, row_count=3)

    response = client.get("/api/blast/jobs", params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 2
    assert body["page"] == {"limit": 2, "returned": 2, "has_more": True}
    # Fetch-one-extra probe: the repo was asked for limit + 1.
    assert seen["limit"] == 3
    # The extra probe row never reaches the client.
    assert "next_cursor" not in body["page"]


def test_jobs_list_page_envelope_has_more_false_on_last_page(monkeypatch) -> None:
    """When the matching set fits within ``limit`` the envelope reports
    ``has_more=False`` and returns every row."""
    client, seen = _pagination_route_setup(monkeypatch, row_count=3)

    response = client.get("/api/blast/jobs", params={"limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert len(body["jobs"]) == 3
    assert body["page"] == {"limit": 5, "returned": 3, "has_more": False}
    assert seen["limit"] == 6


def _cursor_route_setup(monkeypatch, *, row_count: int):
    """Wire a fake repo whose ``list_owner_page`` honours the keyset cursor.

    ``rows`` are newest-first (``job-0`` is the latest ``created_at``). The fake
    mirrors the real repo: the first page (empty cursor) goes through
    ``list_for_owner`` while a non-empty cursor steers ``list_owner_page`` and
    filters ``RowKey gt cursor`` (i.e. strictly older than the boundary). The
    time-index flag is forced ON so the route emits ``next_cursor``.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")

    import api.services.blast.jobs_list_cache as cache_mod
    import api.services.state_repo as state_repo_mod
    from api.routes.blast import jobs as jobs_mod
    from api.services.state.time_index import decode_cursor, row_key

    cache_mod.reset_jobs_list_cache()

    rows = [
        SimpleNamespace(
            job_id=f"job-{i}",
            type="blast",
            status="completed",
            phase="completed",
            owner_oid="00000000-0000-0000-0000-000000000000",
            created_at=f"2026-06-{(row_count - i):02d}T00:00:00Z",
        )
        for i in range(row_count)
    ]
    # Index order: ascending RowKey == newest-first (inverted ticks).
    indexed = sorted(rows, key=lambda r: row_key(r.created_at, r.job_id))
    calls: dict[str, object] = {}

    class Repo:
        def list_for_owner(self, owner_oid, limit=50, *, include_payload=True):
            calls["first_page_limit"] = limit
            return indexed[:limit]

        def list_owner_page(
            self, owner_oid, *, limit=50, include_payload=True, cursor=""
        ):
            calls["cursor"] = cursor
            calls["page_limit"] = limit
            boundary = decode_cursor(cursor)
            window = (
                [r for r in indexed if row_key(r.created_at, r.job_id) > boundary]
                if boundary
                else indexed
            )
            return window[:limit], None

    monkeypatch.setattr(state_repo_mod, "get_state_repo", lambda: Repo())
    monkeypatch.setattr(
        jobs_mod,
        "_local_to_blast_job",
        lambda row, **_kwargs: {
            "job_id": row.job_id,
            "status": row.status,
            "created_at": row.created_at,
        },
    )
    monkeypatch.setattr(jobs_mod, "_local_state_matches_job_scope", lambda *a, **k: True)
    monkeypatch.setattr(
        jobs_mod, "_local_list_row_may_have_split_children", lambda _row: False
    )
    monkeypatch.setattr(jobs_mod, "_blocked_refresh_reasons", lambda _rows: {})
    monkeypatch.setattr(
        jobs_mod,
        "collect_and_sync_external_jobs",
        lambda **_kwargs: SimpleNamespace(
            rows=[], tombstoned_ids=set(), any_target_ok=True, target_failures=[]
        ),
    )

    from api.main import app

    return TestClient(app), calls, rows


def test_jobs_list_emits_next_cursor_keyset_of_last_row(monkeypatch) -> None:
    """With the time index on, a full page carries ``next_cursor`` equal to the
    keyset (encoded RowKey) of the LAST displayed row, not the fetch cursor."""
    from api.services.state.time_index import encode_cursor, row_key

    client, calls, rows = _cursor_route_setup(monkeypatch, row_count=5)

    body = client.get("/api/blast/jobs", params={"limit": 2}).json()

    assert [j["job_id"] for j in body["jobs"]] == ["job-0", "job-1"]
    assert body["page"]["has_more"] is True
    expected = encode_cursor(row_key(rows[1].created_at, "job-1"))
    assert body["page"]["next_cursor"] == expected
    # First page goes through list_for_owner (cursor empty → not paginating).
    assert "cursor" not in calls


def test_jobs_list_cursor_page_continues_without_overlap(monkeypatch) -> None:
    """Passing the prior page's ``next_cursor`` steers ``list_owner_page`` and
    returns strictly-older rows — no overlap with, and no gap after, page one."""
    from api.services.state.time_index import encode_cursor, row_key

    client, calls, rows = _cursor_route_setup(monkeypatch, row_count=5)

    cursor = encode_cursor(row_key(rows[1].created_at, "job-1"))
    body = client.get(
        "/api/blast/jobs", params={"limit": 2, "cursor": cursor}
    ).json()

    # Page two = the next two strictly-older rows, no overlap with [job-0, job-1].
    assert [j["job_id"] for j in body["jobs"]] == ["job-2", "job-3"]
    assert body["page"]["has_more"] is True
    assert calls["cursor"] == cursor
    # next_cursor advances to the last displayed row of page two.
    assert body["page"]["next_cursor"] == encode_cursor(
        row_key(rows[3].created_at, "job-3")
    )


def test_jobs_list_no_next_cursor_when_index_disabled(monkeypatch) -> None:
    """With the time-index flag OFF the route never emits ``next_cursor`` even
    when ``has_more`` is true — pagination stays first-page-only."""
    monkeypatch.delenv("JOBSTATE_TIME_INDEX_ENABLED", raising=False)
    client, _seen = _pagination_route_setup(monkeypatch, row_count=3)

    body = client.get("/api/blast/jobs", params={"limit": 2}).json()

    assert body["page"]["has_more"] is True
    assert "next_cursor" not in body["page"]


def test_jobs_list_scoped_listing_has_no_cursor(monkeypatch) -> None:
    """A scoped listing (mutable-column scan) is first-page-only: it ignores an
    inbound cursor and never emits ``next_cursor`` even with the index flag on."""
    monkeypatch.setenv("JOBSTATE_TIME_INDEX_ENABLED", "true")
    client, _seen = _pagination_route_setup(monkeypatch, row_count=3)

    body = client.get(
        "/api/blast/jobs",
        params={"limit": 2, "cluster_name": "elb-cluster-01", "cursor": "x"},
    ).json()

    assert body["page"]["has_more"] is True
    assert "next_cursor" not in body["page"]


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


# --- Job detail metadata & cache scenarios (queue/API enrichment) -----------
# Integration coverage for the sibling-stats merge + OPS-Redis cache on the
# detail route. Scenario IDs map to
# `.github/skills/blast-execution-validation/references/scenario-matrix.md`
# "Job Detail Metadata & Cache Scenarios".


class _FakeOpsRedis:
    """In-memory stand-in for the OPS Redis client (ignores TTL)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)


def _completed_external_state(job_id: str = "ext-done"):
    """A completed Service Bus job row whose stored row carries no execution
    stats (db_version / blast_version / run_seconds) — the detail merges them
    live from the sibling and caches the result."""
    return SimpleNamespace(
        job_id=job_id,
        task_id=None,
        type="blast",
        owner_oid="00000000-0000-0000-0000-000000000000",
        status="completed",
        phase="completed",
        created_at="2026-06-20T00:00:00Z",
        updated_at="2026-06-20T00:05:00Z",
        error_code="",
        parent_job_id=None,
        subscription_id="sub-1",
        resource_group="rg-1",
        cluster_name="elb-cluster-01",
        storage_account="",
        payload={
            "db": "core_nt",
            "external": {"job_id": job_id, "submission_source": "servicebus"},
        },
    )


def _detail_test_harness(monkeypatch, fake_get_job):
    """Wire AUTH bypass, a completed external repo row, the region mock, a fake
    OPS Redis, and the sibling get_job stub. Returns (client, calls, fake)."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.services.blast.external_config.resolve_cluster_region",
        lambda *a, **k: "koreacentral",
    )
    state = _completed_external_state()

    class Repo:
        def get(self, job_id: str):
            return state

        def list_children(self, *_a, **_k):
            return []

    monkeypatch.setattr("api.services.state_repo.JobStateRepository", Repo)
    fake = _FakeOpsRedis()
    monkeypatch.setattr(
        "api.services.redis_clients.get_ops_redis_client", lambda: fake, raising=False
    )
    from api.services import external_blast

    monkeypatch.setattr(external_blast, "get_job", fake_get_job)

    from api.main import app

    return TestClient(app), fake


def test_job_detail_merges_sibling_stats_and_caches(monkeypatch) -> None:
    """Scenario #6: a completed external job missing db_version merges the
    sibling stats on the FIRST detail load (one live get_job) and serves the
    SECOND load from the OPS-Redis cache (no second get_job)."""
    calls: list[str] = []

    def fake_get_job(job_id, **_k):
        calls.append(job_id)
        return {
            "job_id": job_id,
            "status": "completed",
            "db_version": "2026-06-06-01-05-02",
            "blast_version": "2.17.0+",
            "run_seconds": 95,
        }

    client, _fake = _detail_test_harness(monkeypatch, fake_get_job)

    r1 = client.get(
        "/api/blast/jobs/ext-done", params={"include_database_metadata": "false"}
    )
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["db_version"] == "2026-06-06-01-05-02"
    assert b1["blast_version"] == "2.17.0+"
    assert b1["run_seconds"] == 95
    assert calls == ["ext-done"]  # exactly one live fetch

    r2 = client.get(
        "/api/blast/jobs/ext-done", params={"include_database_metadata": "false"}
    )
    assert r2.status_code == 200
    assert r2.json()["db_version"] == "2026-06-06-01-05-02"
    assert calls == ["ext-done"]  # cache hit — no second get_job


def test_job_detail_sibling_stats_negative_marker_bounds_refetch(monkeypatch) -> None:
    """Scenario #9: when the sibling is unreachable (Stopped cluster → get_job
    raises) the detail still renders 200 (stats show "—"), and a short-lived
    negative marker bounds the re-fetch so the next load within the TTL does NOT
    re-pay the timeout."""
    calls: list[str] = []

    def fake_get_job(job_id, **_k):
        calls.append(job_id)
        raise RuntimeError("cluster API server unreachable (timeout)")

    client, _fake = _detail_test_harness(monkeypatch, fake_get_job)

    r1 = client.get(
        "/api/blast/jobs/ext-done", params={"include_database_metadata": "false"}
    )
    assert r1.status_code == 200  # detail renders despite the timeout
    assert not r1.json().get("db_version")
    assert calls == ["ext-done"]

    r2 = client.get(
        "/api/blast/jobs/ext-done", params={"include_database_metadata": "false"}
    )
    assert r2.status_code == 200
    assert calls == ["ext-done"]  # negative marker prevented the re-fetch


def test_job_detail_legacy_external_row_renders_without_stats(monkeypatch) -> None:
    """Scenario #11: a legacy external job whose sibling reports no usable stats
    renders gracefully (null optional fields, no 500) and caches a negative
    marker so it stops re-fetching."""
    calls: list[str] = []

    def fake_get_job(job_id, **_k):
        calls.append(job_id)
        return {"job_id": job_id, "status": "completed"}  # no stats at all

    client, _fake = _detail_test_harness(monkeypatch, fake_get_job)

    r1 = client.get(
        "/api/blast/jobs/ext-done", params={"include_database_metadata": "false"}
    )
    assert r1.status_code == 200
    b1 = r1.json()
    assert not b1.get("db_version")
    assert not b1.get("config_snapshot")  # legacy: nothing captured
    assert not b1.get("query_length")
    assert not b1.get("molecule")
    assert calls == ["ext-done"]

    r2 = client.get(
        "/api/blast/jobs/ext-done", params={"include_database_metadata": "false"}
    )
    assert r2.status_code == 200
    assert calls == ["ext-done"]  # negative marker bounds the re-fetch

