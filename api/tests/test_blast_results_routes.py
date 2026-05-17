"""HTTP-level tests for the BLAST results endpoints.

Validate that the wired routes (`/api/blast/jobs/{id}/results/aggregate`,
`/alignments`, `/export`) parse blobs from storage and return the shape the
SPA expects, including filter behaviour and CSV / TSV / JSON export.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

_OUTFMT6 = (
    "queryA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\n"
    "queryA\tNC_002\t95.0\t148\t7\t1\t1\t148\t200\t347\t2e-40\t250\n"
    "queryB\tNC_001\t100.0\t120\t0\t0\t1\t120\t1\t120\t3e-60\t320\n"
)


@pytest.fixture
def patched_storage(monkeypatch: pytest.MonkeyPatch):
    """Pre-load `_RESULTS_*` helpers with deterministic blob listings.

    Returns a small container that the test can mutate to control what
    `list_result_blobs` and `read_blob_text` return.
    """
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    from api.services import storage_data

    state: dict[str, Any] = {
        "blobs": [{"name": "job123/results.out", "size": len(_OUTFMT6)}],
        "content": _OUTFMT6,
    }

    def fake_list(_cred, _account, container, prefix):
        assert container == "results"
        return [b for b in state["blobs"] if b["name"].startswith(prefix)]

    def fake_read(_cred, _account, container, blob_path, max_bytes=4096):
        assert container == "results"
        return state["content"]

    monkeypatch.setattr(storage_data, "list_result_blobs", fake_list)
    monkeypatch.setattr(storage_data, "read_blob_text", fake_read)
    # Also patch the api.services namespace alias used by the route.
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    return state


def test_aggregate_returns_stats_shape(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/aggregate",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["files_parsed"] == 1
    assert body["total_files"] == 1
    stats = body["stats"]
    assert stats["total_hits"] == 3
    assert stats["unique_queries"] == 2
    assert stats["unique_subjects"] == 2
    assert "evalue_distribution" in stats
    assert "identity_distribution" in stats
    assert "top_subjects" in stats
    assert "top_hit_per_query" in stats


def test_aggregate_empty_listing_returns_no_results(monkeypatch, patched_storage):
    patched_storage["blobs"] = []
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/empty/results/aggregate",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_results"
    assert body["stats"] is None


def test_alignments_filter_by_query_id(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "query_id": "queryB"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_hits"] == 3
    assert body["filtered_hits"] == 1
    assert body["alignments"][0]["qseqid"] == "queryB"
    assert "queryA" in body["query_ids"]


def test_alignments_filter_by_min_identity(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "min_identity": 99.0},
    )
    assert response.status_code == 200
    body = response.json()
    # 99.5 + 100.0 → 2 hits; 95.0 → dropped.
    assert body["filtered_hits"] == 2


def test_alignments_rejects_path_traversal(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "blob_name": "../secrets.txt"},
    )
    assert response.status_code == 400


def test_alignments_rejects_backslash_traversal(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "blob_name": "job123\\..\\secrets.txt"},
    )
    assert response.status_code == 400


def test_alignments_rejects_url_encoded_traversal(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "blob_name": "job123/%2e%2e/secrets.txt"},
    )
    assert response.status_code == 400


def test_export_csv_has_header_and_rows(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "csv"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    text = response.text
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert len(rows) == 3
    assert "qseqid" in reader.fieldnames
    assert "bitscore" in reader.fieldnames


def test_export_tsv_uses_tab_delimiter(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "tsv"},
    )
    assert response.status_code == 200
    assert "tab-separated" in response.headers["content-type"]
    first_line = response.text.splitlines()[0]
    assert "\t" in first_line
    assert "," not in first_line


def test_export_json_returns_hits_array(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "json"},
    )
    assert response.status_code == 200
    payload = json.loads(response.text)
    assert payload["job_id"] == "job123"
    assert payload["total"] == 3
    assert len(payload["hits"]) == 3


def test_export_rejects_unknown_format(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "xml"},
    )
    assert response.status_code == 422


def test_aggregate_degraded_when_all_reads_fail(monkeypatch, patched_storage):
    """If every result blob read raises, surface a 'degraded' status.

    Returning 'no_hits' here would lie to the researcher — the blobs exist
    but storage is unreachable / RBAC missing.
    """
    from api.main import app
    from api.services import storage_data

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated 403")

    monkeypatch.setattr(storage_data, "read_blob_text", boom)
    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/aggregate",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["degraded_reason"] == "all_reads_failed"
    assert payload["read_failures"] >= 1


def test_export_degraded_when_all_reads_fail(monkeypatch, patched_storage):
    """Export must NOT silently produce header-only CSV when every read fails."""
    from api.main import app
    from api.services import storage_data

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated 403")

    monkeypatch.setattr(storage_data, "read_blob_text", boom)
    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "csv"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "all_reads_failed"
