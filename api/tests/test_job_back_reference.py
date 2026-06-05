"""Tests for the record→job back-reference lookup (service + route).

Responsibility: Cover accession matching, version normalisation, sub-range projection,
non-accession/soft-deleted exclusion, owner isolation, the `limit` cap, and the route's
degraded fallback for the "Your BLAST jobs for this accession" card.
Edit boundaries: One behaviour family (job back-reference). Add new cases here rather than
broadening unrelated job-list tests.
Key entry points: pytest test functions.
Risky contracts: The degraded path must return 200 (never 500) so Sequence Detail never breaks.
Validation: `uv run pytest -q api/tests/test_job_back_reference.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from api.services.blast.job_back_reference import (
    accession_base,
    find_jobs_for_accession,
)
from fastapi.testclient import TestClient

DEV_BYPASS_OID = "00000000-0000-0000-0000-000000000000"


def _job(
    job_id: str,
    *,
    accession: str | None,
    status: str = "completed",
    phase: str | None = "succeeded",
    db: str = "core_nt",
    query_source: str = "ncbi_accession",
    seq_start: int | None = None,
    seq_stop: int | None = None,
    created_at: str = "2026-05-30T08:12:00+00:00",
    job_type: str = "blast",
) -> SimpleNamespace:
    query_metadata: dict[str, Any] = {}
    if accession is not None:
        query_metadata["query_source"] = query_source
        query_metadata["query_accession"] = accession
        if seq_start is not None:
            query_metadata["query_accession_seq_start"] = seq_start
        if seq_stop is not None:
            query_metadata["query_accession_seq_stop"] = seq_stop
    payload = {"db": db}
    if query_metadata:
        payload["query_metadata"] = query_metadata
    return SimpleNamespace(
        job_id=job_id,
        type=job_type,
        status=status,
        phase=phase,
        db=db,
        created_at=created_at,
        payload=payload,
    )


class _FakeRepo:
    """Minimal jobstate repo: returns the configured rows, newest-first.

    Mirrors ``list_for_owner``'s contract that soft-deleted rows are already
    filtered out (the real repo's OData filter excludes ``status='deleted'``),
    so tests pass only the rows the caller would actually see.
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.last_kwargs: dict[str, Any] | None = None

    def list_for_owner(
        self, owner_oid: str, limit: int = 50, *, include_payload: bool = True
    ) -> list[Any]:
        self.last_kwargs = {
            "owner_oid": owner_oid,
            "limit": limit,
            "include_payload": include_payload,
        }
        return list(self._rows)


def test_accession_base_strips_version_and_uppercases() -> None:
    assert accession_base("NM_000546.6") == "NM_000546"
    assert accession_base("nm_000546") == "NM_000546"
    assert accession_base("  NR_12345.2  ") == "NR_12345"


def test_match_base_normalises_version_and_case() -> None:
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    rows = find_jobs_for_accession(repo, "owner-1", "nm_000546", match="base")
    assert [r["job_id"] for r in rows] == ["j1"]
    assert rows[0]["query_accession"] == "NM_000546.6"


def test_match_exact_requires_full_accession_version() -> None:
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    assert find_jobs_for_accession(repo, "o", "NM_000546.5", match="exact") == []
    rows = find_jobs_for_accession(repo, "o", "nm_000546.6", match="exact")
    assert [r["job_id"] for r in rows] == ["j1"]


def test_sub_range_projection_carries_start_stop() -> None:
    repo = _FakeRepo([_job("j1", accession="NM_000546.6", seq_start=100, seq_stop=200)])
    rows = find_jobs_for_accession(repo, "o", "NM_000546.6")
    assert rows[0]["seq_start"] == 100
    assert rows[0]["seq_stop"] == 200


def test_whole_sequence_job_has_null_range() -> None:
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    rows = find_jobs_for_accession(repo, "o", "NM_000546.6")
    assert rows[0]["seq_start"] is None
    assert rows[0]["seq_stop"] is None


def test_non_accession_jobs_excluded() -> None:
    repo = _FakeRepo(
        [
            _job("paste", accession="NM_000546.6", query_source="inline_fasta"),
            _job("none", accession=None),
            _job("hit", accession="NM_000546.6"),
        ]
    )
    rows = find_jobs_for_accession(repo, "o", "NM_000546.6")
    assert [r["job_id"] for r in rows] == ["hit"]


def test_non_blast_rows_excluded() -> None:
    repo = _FakeRepo([_job("aks", accession="NM_000546.6", job_type="aks_provision")])
    assert find_jobs_for_accession(repo, "o", "NM_000546.6") == []


def test_other_accession_not_matched() -> None:
    repo = _FakeRepo([_job("j1", accession="NR_999999.1")])
    assert find_jobs_for_accession(repo, "o", "NM_000546.6") == []


def test_limit_truncates_after_match() -> None:
    rows_in = [_job(f"j{i}", accession="NM_000546.6") for i in range(5)]
    repo = _FakeRepo(rows_in)
    rows = find_jobs_for_accession(repo, "o", "NM_000546.6", limit=2)
    assert len(rows) == 2
    assert [r["job_id"] for r in rows] == ["j0", "j1"]


def test_scan_reads_payloads_owner_scoped() -> None:
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    find_jobs_for_accession(repo, "owner-xyz", "NM_000546.6")
    assert repo.last_kwargs is not None
    assert repo.last_kwargs["owner_oid"] == "owner-xyz"
    assert repo.last_kwargs["include_payload"] is True


def test_database_blob_url_subscription_id_sanitised() -> None:
    sub = "11111111-2222-3333-4444-555555555555"
    blob_db = f"https://acct.blob.core.windows.net/{sub}/core_nt"
    repo = _FakeRepo([_job("j1", accession="NM_000546.6", db=blob_db)])
    rows = find_jobs_for_accession(repo, "o", "NM_000546.6")
    assert sub not in rows[0]["database"]


# --- route contract -------------------------------------------------------


def test_route_returns_owner_jobs(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/blast/jobs/by-accession/NM_000546.6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["accession"] == "NM_000546.6"
    assert body["accession_base"] == "NM_000546"
    assert body["match"] == "base"
    assert body["count"] == 1
    assert body["degraded"] is False
    assert body["jobs"][0]["job_id"] == "j1"
    assert body["jobs"][0]["detail_url"] == "/blast/jobs/j1"
    # The route must scope to the dev-bypass caller, not a wildcard.
    assert repo.last_kwargs is not None
    assert repo.last_kwargs["owner_oid"] == DEV_BYPASS_OID


def test_route_requires_auth(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/blast/jobs/by-accession/NM_000546.6")
    assert resp.status_code in (401, 403)


def test_route_caps_limit(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    from api.main import app

    client = TestClient(app)
    assert client.get("/api/blast/jobs/by-accession/NM_000546.6?limit=51").status_code == 422
    assert client.get("/api/blast/jobs/by-accession/NM_000546.6?match=bad").status_code == 422


def test_route_degrades_to_200_on_repo_failure(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    class _Broken:
        def list_for_owner(self, *a: Any, **k: Any) -> list[Any]:
            raise RuntimeError("table outage")

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Broken())

    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/blast/jobs/by-accession/NM_000546.6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["reason"] == "jobstate_unavailable"
    assert body["jobs"] == []


def test_route_does_not_shadow_job_id_path(monkeypatch) -> None:
    """`/jobs/by-accession/{accession}` must not be captured as `/jobs/{job_id}`."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    repo = _FakeRepo([_job("j1", accession="NM_000546.6")])
    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: repo)

    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/blast/jobs/by-accession/NM_000546.6")
    assert resp.status_code == 200
    # The by-accession envelope (not the single-job projection) must come back.
    assert "accession_base" in resp.json()
