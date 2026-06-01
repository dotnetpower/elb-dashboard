"""Unit + HTTP tests for the NCBI Web BLAST-style report export.

Responsibility: Verify per-subject aggregation rules and the `ncbi-*` export
formats on `/api/blast/jobs/{id}/results/export`.
Edit boundaries: Keep assertions focused on aggregation/rendering; use fakes for
Storage instead of live Azure calls.
Key entry points: `test_aggregate_collapses_hsps_per_subject`,
`test_export_ncbi_report_text_has_header`.
Risky contracts: Do not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_blast_ncbi_report.py`.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from api.services.blast.ncbi_report import (
    NCBI_HIT_TABLE_COLUMNS,
    aggregate_ncbi_rows,
    format_ncbi_hit_table,
    format_ncbi_report_text,
)
from fastapi.testclient import TestClient

# Two HSPs against NC_001 (same subject) + one against NC_002.
_HITS = [
    {
        "qseqid": "queryA",
        "sseqid": "NC_001",
        "pident": "99.5",
        "bitscore": "289",
        "evalue": "1e-50",
        "qstart": "1",
        "qend": "150",
        "qlen": "300",
        "slen": "249",
        "stitle": "Homo sapiens chromosome 1",
        "sscinames": "Homo sapiens",
        "staxids": "9606",
    },
    {
        "qseqid": "queryA",
        "sseqid": "NC_001",
        "pident": "88.0",
        "bitscore": "120",
        "evalue": "2e-20",
        "qstart": "200",
        "qend": "260",
        "qlen": "300",
        "slen": "249",
        "stitle": "Homo sapiens chromosome 1",
        "sscinames": "Homo sapiens",
        "staxids": "9606",
    },
    {
        "qseqid": "queryA",
        "sseqid": "NC_002",
        "pident": "95.0",
        "bitscore": "250",
        "evalue": "2e-40",
        "qstart": "1",
        "qend": "148",
        "qlen": "300",
        "slen": "347",
        "stitle": "Pan troglodytes chromosome 2",
        "sscinames": "Pan troglodytes",
        "staxids": "9598",
    },
]


def test_aggregate_collapses_hsps_per_subject() -> None:
    rows = aggregate_ncbi_rows(_HITS)
    assert len(rows) == 2  # NC_001 (2 HSPs) + NC_002
    by_acc = {r.accession: r for r in rows}

    nc001 = by_acc["NC_001"]
    assert nc001.max_score == 289  # max bitscore
    assert nc001.total_score == 409  # 289 + 120
    assert nc001.evalue == 1e-50  # min evalue
    assert nc001.per_ident == 99.5  # identity of the top-scoring HSP
    assert nc001.acc_len == 249
    # Query cover: union of [1,150] and [200,260] = 150 + 61 = 211 / 300 = 70%
    assert nc001.query_cover == 70
    assert nc001.scientific_name == "Homo sapiens"
    assert nc001.taxid == "9606"
    assert nc001.description == "Homo sapiens chromosome 1"


def test_aggregate_sorts_by_query_then_max_score() -> None:
    rows = aggregate_ncbi_rows(_HITS)
    assert [r.accession for r in rows] == ["NC_001", "NC_002"]


def test_aggregate_handles_missing_taxonomy() -> None:
    rows = aggregate_ncbi_rows(
        [{"qseqid": "q", "sseqid": "S1", "bitscore": "10", "evalue": "1", "slen": "100"}]
    )
    assert rows[0].scientific_name == ""
    assert rows[0].taxid == ""
    assert rows[0].description == "S1"  # falls back to accession


def test_format_hit_table_uses_ncbi_columns() -> None:
    rows = aggregate_ncbi_rows(_HITS)
    text = format_ncbi_hit_table(rows, delimiter="\t")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = next(reader)
    assert header == list(NCBI_HIT_TABLE_COLUMNS)
    data = list(reader)
    assert len(data) == 2
    assert any("Homo sapiens" in cell for row in data for cell in row)


def test_format_report_text_has_provenance_header() -> None:
    rows = aggregate_ncbi_rows(_HITS)
    report = format_ncbi_report_text(
        rows,
        rid="ELB-job-1",
        program="blastn",
        database="core_nt",
        job_title="My run",
        blast_version="2.17.0+",
        database_snapshot="2026-05-01",
    )
    assert "RID: ELB-job-1" in report
    assert "Program: BLASTN" in report
    assert "Database: core_nt" in report
    assert "Job Title: My run" in report
    assert "Not an NCBI-issued report" in report
    assert "Query #1: queryA" in report


def test_report_text_never_emits_storage_urls() -> None:
    rows = aggregate_ncbi_rows(_HITS)
    report = format_ncbi_report_text(rows, rid="ELB-x", program="blastn", database="db")
    assert "blob.core.windows.net" not in report
    assert "sig=" not in report


# --- HTTP tests -----------------------------------------------------------

_OUTFMT6 = (
    "queryA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\n"
    "queryA\tNC_001\t88.0\t60\t5\t0\t200\t260\t1\t60\t2e-20\t120\n"
    "queryA\tNC_002\t95.0\t148\t7\t1\t1\t148\t200\t347\t2e-40\t250\n"
)


@pytest.fixture
def patched_storage(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.services.storage import data as storage_data

    state: dict[str, Any] = {
        "blobs": [{"name": "job123/results.out", "size": len(_OUTFMT6)}],
        "content": _OUTFMT6,
    }

    def fake_list(_cred, _account, container, prefix):
        return [b for b in state["blobs"] if b["name"].startswith(prefix)]

    def fake_read(_cred, _account, container, blob_path, max_bytes=4096):
        return state["content"]

    monkeypatch.setattr(storage_data, "list_result_blobs", fake_list)
    monkeypatch.setattr(storage_data, "read_blob_text", fake_read)
    monkeypatch.setattr(storage_data, "read_result_blob_text", fake_read)
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    return state


def test_export_ncbi_hit_table_text(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "ncbi-hit-table-text"},
    )
    assert response.status_code == 200
    assert "tab-separated" in response.headers["content-type"]
    header = response.text.splitlines()[0]
    assert "Max Score" in header
    assert "Per. Ident" in header
    assert "\t" in header


def test_export_ncbi_hit_table_csv(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "ncbi-hit-table-csv"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    # NC_001 collapses 2 HSPs -> 1 row; NC_002 -> 1 row.
    assert len(rows) == 2
    assert "Accession" in rows[0]


def test_export_ncbi_report_text_has_header(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "ncbi-report-text"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "RID: ELB-job123" in body
    assert "Sequences producing significant alignments" in body
    assert "Query #1: queryA" in body
