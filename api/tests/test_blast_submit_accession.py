"""Tests for accession-mode BLAST submit (resolver + normalise_body wiring).

Responsibility: Lock the contract between `query_accession` on the submit body
and the existing `query_data` upload path. Cover happy path, missing storage,
NCBI outage, invalid accession, partial subrange, and the precedence rule that
explicit `query_data` / `query_file` wins over `query_accession`.
Edit boundaries: Only `api.services.blast.accession_resolver` and the
accession branch of `_normalise_blast_submit_body`.
Key entry points: `resolve_accession_to_fasta`, `_normalise_blast_submit_body`.
Risky contracts: No live network — every test stubs
`api.services.ncbi.fetch_nuccore_fasta` and the storage upload.
Validation: `uv run pytest -q api/tests/test_blast_submit_accession.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException


def _stub_upload(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    from api.services.blast import submit_payload

    def fake_upload(*, job_id: str, storage_account: str, query_data: str):
        captured.append(
            {
                "job_id": job_id,
                "storage_account": storage_account,
                "query_data": query_data,
            }
        )
        return (
            f"queries/uploads/{job_id}/query.fa",
            {"query_count": 1, "total_letters": len(query_data) - 1},
        )

    monkeypatch.setattr(
        submit_payload, "_upload_inline_query_for_submit", fake_upload
    )
    return captured


def _stub_fetch_fasta(
    monkeypatch: pytest.MonkeyPatch, text: str = ">NM_000546.6 fake\nACGTACGT\n"
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    from api.services import ncbi

    def fake_fetch(
        accession: str,
        *,
        seq_start: int | None = None,
        seq_stop: int | None = None,
    ) -> str:
        seen.append(
            {"accession": accession, "seq_start": seq_start, "seq_stop": seq_stop}
        )
        return text

    monkeypatch.setattr(ncbi, "fetch_nuccore_fasta", fake_fetch)
    from api.services.ncbi import nuccore

    monkeypatch.setattr(nuccore, "fetch_nuccore_fasta", fake_fetch)
    return seen


# ---------------------------------------------------------------------------
# Service: resolve_accession_to_fasta
# ---------------------------------------------------------------------------
def test_resolver_returns_text_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_fetch_fasta(monkeypatch)
    from api.services.blast.accession_resolver import resolve_accession_to_fasta

    text, meta = resolve_accession_to_fasta("nm_000546.6")
    assert text.startswith(">NM_000546.6")
    assert seen == [
        {"accession": "NM_000546.6", "seq_start": None, "seq_stop": None}
    ]
    assert meta == {"query_source": "ncbi_accession", "query_accession": "NM_000546.6"}


def test_resolver_forwards_subrange(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_fetch_fasta(monkeypatch)
    from api.services.blast.accession_resolver import resolve_accession_to_fasta

    text, meta = resolve_accession_to_fasta(
        "NM_000546.6", seq_start=100, seq_stop=200
    )
    assert text.startswith(">")
    assert seen[0]["seq_start"] == 100
    assert seen[0]["seq_stop"] == 200
    assert meta["query_accession_seq_start"] == 100
    assert meta["query_accession_seq_stop"] == 200


def test_resolver_rejects_partial_subrange(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fetch_fasta(monkeypatch)
    from api.services.blast.accession_resolver import resolve_accession_to_fasta

    with pytest.raises(HTTPException) as ctx:
        resolve_accession_to_fasta("NM_000546.6", seq_start=100)
    assert ctx.value.status_code == 422
    detail = ctx.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "ncbi_accession_invalid"


def test_resolver_rejects_invalid_accession(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fetch_fasta(monkeypatch)
    from api.services.blast.accession_resolver import resolve_accession_to_fasta

    with pytest.raises(HTTPException) as ctx:
        resolve_accession_to_fasta("not-an-accession")
    assert ctx.value.status_code == 422


def test_resolver_maps_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.services import ncbi
    from api.services.blast.accession_resolver import resolve_accession_to_fasta

    def boom(*_a: Any, **_kw: Any) -> str:
        raise ncbi.NcbiServiceUnavailable("eutils down")

    monkeypatch.setattr(ncbi, "fetch_nuccore_fasta", boom)
    from api.services.ncbi import nuccore

    monkeypatch.setattr(nuccore, "fetch_nuccore_fasta", boom)

    with pytest.raises(HTTPException) as ctx:
        resolve_accession_to_fasta("NM_000546.6")
    assert ctx.value.status_code == 503
    detail = ctx.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "ncbi_lookup_unavailable"
    assert detail["retryable"] is True


def test_resolver_rejects_zero_subrange(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fetch_fasta(monkeypatch)
    from api.services.blast.accession_resolver import resolve_accession_to_fasta

    with pytest.raises(HTTPException):
        resolve_accession_to_fasta("NM_000546.6", seq_start=0, seq_stop=10)


# ---------------------------------------------------------------------------
# Wiring: _normalise_blast_submit_body
# ---------------------------------------------------------------------------
def _base_submit_body() -> dict[str, Any]:
    return {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "resource_group": "rg-test",
        "cluster_name": "cluster-test",
        "storage_account": "stelbtest01",
        "program": "blastn",
        "database": "blob://stelbtest01/blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA",
        "db": "blob://stelbtest01/blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA",
    }


def test_normalise_accession_branch_invokes_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    uploads = _stub_upload(monkeypatch)
    seen = _stub_fetch_fasta(
        monkeypatch, text=">NM_000546.6 ACGT\nACGTACGTACGT\n"
    )
    from api.routes._blast_shared import _normalise_blast_submit_body

    body = {
        **_base_submit_body(),
        "query_accession": "NM_000546.6",
        "query_accession_seq_start": 100,
        "query_accession_seq_stop": 200,
    }
    result = _normalise_blast_submit_body(body, job_id="job-abc")

    assert seen == [
        {"accession": "NM_000546.6", "seq_start": 100, "seq_stop": 200}
    ]
    assert uploads == [
        {
            "job_id": "job-abc",
            "storage_account": "stelbtest01",
            "query_data": ">NM_000546.6 ACGT\nACGTACGTACGT\n",
        }
    ]
    assert result["query_file"] == "queries/uploads/job-abc/query.fa"
    # The accession-only inputs must NOT leak into the downstream payload.
    assert "query_accession" not in result
    assert "query_accession_seq_start" not in result
    assert "query_accession_seq_stop" not in result
    assert "query_data" not in result
    # Provenance is preserved in query_metadata.
    meta = result["query_metadata"]
    assert meta["query_source"] == "ncbi_accession"
    assert meta["query_accession"] == "NM_000546.6"
    assert meta["query_accession_seq_start"] == 100
    assert meta["query_accession_seq_stop"] == 200


def test_normalise_query_data_conflicts_with_accession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed query sources must raise 422 instead of silently picking one.

    Silent precedence makes audit + replay ambiguous (two callers sending
    the same body would get the same job, but with different actual
    queries). The submit normaliser rejects the mix so OpenAPI, dashboard,
    and external callers all see the same error.
    """
    _stub_upload(monkeypatch)
    seen = _stub_fetch_fasta(monkeypatch)
    from api.routes._blast_shared import _normalise_blast_submit_body

    body = {
        **_base_submit_body(),
        "query_accession": "NM_000546.6",
        "query_data": ">manual ACGT\nACGT\n",
    }
    with pytest.raises(HTTPException) as ctx:
        _normalise_blast_submit_body(body, job_id="job-xyz")
    assert ctx.value.status_code == 422
    detail = ctx.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "conflicting_query_sources"
    # Accession resolution must not be attempted when we know the input
    # is ambiguous.
    assert seen == []


def test_normalise_query_file_conflicts_with_accession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upload(monkeypatch)
    seen = _stub_fetch_fasta(monkeypatch)
    from api.routes._blast_shared import _normalise_blast_submit_body

    body = {
        **_base_submit_body(),
        "query_accession": "NM_000546.6",
        "query_file": "queries/already-staged.fa",
    }
    with pytest.raises(HTTPException) as ctx:
        _normalise_blast_submit_body(body, job_id="job-pre")
    assert ctx.value.status_code == 422
    detail = ctx.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "conflicting_query_sources"
    assert seen == []


def test_normalise_accession_partial_subrange_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upload(monkeypatch)
    _stub_fetch_fasta(monkeypatch)
    from api.routes._blast_shared import _normalise_blast_submit_body

    body = {
        **_base_submit_body(),
        "query_accession": "NM_000546.6",
        "query_accession_seq_start": 100,
    }
    with pytest.raises(HTTPException) as ctx:
        _normalise_blast_submit_body(body, job_id="job-bad")
    assert ctx.value.status_code == 422


def test_normalise_accession_requires_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_upload(monkeypatch)
    _stub_fetch_fasta(monkeypatch)
    from api.routes._blast_shared import _normalise_blast_submit_body

    body = {**_base_submit_body(), "query_accession": "NM_000546.6"}
    body["storage_account"] = ""
    with pytest.raises(HTTPException) as ctx:
        _normalise_blast_submit_body(body, job_id="job-ns")
    assert ctx.value.status_code == 422
    detail = ctx.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Pre-side-effect precision contract: precise sharding must be eligible for an
# accession-only submit, since the accession resolves to a single query and is
# only fetched later in `_normalise_blast_submit_body`.
# ---------------------------------------------------------------------------
def test_submit_contracts_precise_sharding_allows_accession_query() -> None:
    from api.services.blast.submit_payload import submit_contracts

    body = {
        **_base_submit_body(),
        "query_accession": "OZ254605.1",
        "sharding_mode": "precise",
        "db_effective_search_space": 32156241807668,
        "db_total_letters": 32156241807668,
        "shard_sets": [4],
    }
    contracts = submit_contracts(body)
    precision = contracts["precision"]
    assert precision["eligible"] is True, precision.get("blocking_errors")
    assert "precise sharding requires query metadata" not in (
        precision.get("blocking_errors") or []
    )


def test_canonical_query_reports_single_count_for_accession() -> None:
    from api.services.blast.submit_payload import _canonical_query_from_body

    query = _canonical_query_from_body(
        {**_base_submit_body(), "query_accession": "OZ254605.1"}
    )
    assert query["kind"] == "ncbi_accession"
    assert query["query_count"] == 1
    assert query["accession"] == "OZ254605.1"
