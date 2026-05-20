"""HTTP-level tests for the BLAST results endpoints.

Responsibility: HTTP-level tests for the BLAST results endpoints
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `patched_storage`, `test_aggregate_returns_stats_shape`,
`test_results_list_opens_storage_for_local_debug_when_scope_present`,
`test_aggregate_empty_listing_returns_no_results`,
`test_alignments_empty_listing_returns_degraded_no_result_files`,
`test_aggregate_no_hits_returns_complete_stats_shape`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py`.
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

_OUTFMT5_XML = """<?xml version="1.0"?>
<BlastOutput>
    <BlastOutput_iterations>
        <Iteration>
            <Iteration_query-ID>queryXml</Iteration_query-ID>
            <Iteration_query-len>100</Iteration_query-len>
            <Iteration_hits>
                <Hit>
                    <Hit_id>gb|XML123.1|</Hit_id>
                    <Hit_accession>XML123</Hit_accession>
                    <Hit_def>XML subject</Hit_def>
                    <Hit_len>500</Hit_len>
                    <Hit_hsps><Hsp>
                        <Hsp_identity>99</Hsp_identity><Hsp_align-len>100</Hsp_align-len>
                        <Hsp_gaps>0</Hsp_gaps><Hsp_query-from>1</Hsp_query-from>
                        <Hsp_query-to>100</Hsp_query-to><Hsp_hit-from>20</Hsp_hit-from>
                        <Hsp_hit-to>119</Hsp_hit-to><Hsp_evalue>2e-40</Hsp_evalue>
                        <Hsp_bit-score>180.5</Hsp_bit-score><Hsp_score>100</Hsp_score>
                        <Hsp_qseq>ACGT</Hsp_qseq><Hsp_hseq>ACGT</Hsp_hseq>
                        <Hsp_midline>||||</Hsp_midline>
                    </Hsp></Hit_hsps>
                </Hit>
            </Iteration_hits>
        </Iteration>
    </BlastOutput_iterations>
</BlastOutput>
"""


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
        if isinstance(state["content"], dict):
            return state["content"][blob_path]
        return state["content"]

    monkeypatch.setattr(storage_data, "list_result_blobs", fake_list)
    monkeypatch.setattr(storage_data, "read_blob_text", fake_read)
    monkeypatch.setattr(storage_data, "read_result_blob_text", fake_read)
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


def test_results_list_opens_storage_for_local_debug_when_scope_present(
    monkeypatch: pytest.MonkeyPatch,
    patched_storage,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_access(_cred: Any, subscription_id: str, resource_group: str, account_name: str):
        calls.append((subscription_id, resource_group, account_name))
        return {"action": "already_open"}

    monkeypatch.setattr(
        "api.services.storage_public_access.ensure_local_storage_access",
        fake_access,
        raising=True,
    )
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-elb-01",
            "storage_account": "stelb",
        },
    )
    assert response.status_code == 200
    assert response.json()["manifest"]["status"] == "available"
    assert response.json()["manifest"]["parseable_count"] == 1
    assert calls == [("00000000-0000-0000-0000-000000000001", "rg-elb-01", "stelb")]


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


def test_alignments_empty_listing_returns_degraded_no_result_files(patched_storage):
    patched_storage["blobs"] = []
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/empty/results/alignments",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["alignments"] == []
    assert body["degraded"] is True
    assert body["degraded_reason"] == "no_result_files"
    assert body["pages"] == 0
    assert body["files_parsed"] == 0


def test_aggregate_no_hits_returns_complete_stats_shape(patched_storage):
    patched_storage["content"] = ""
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/aggregate",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_hits"
    assert body["stats"]["total_hits"] == 0
    assert body["stats"]["evalue_distribution"]
    assert body["stats"]["identity_distribution"]


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


def test_alignments_parse_xml_gzip_result_blob(patched_storage):
    patched_storage["blobs"] = [{"name": "job123/merged_results.out.gz", "size": 123}]
    patched_storage["content"] = _OUTFMT5_XML
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["blob_name"] == "job123/merged_results.out.gz"
    assert body["total_hits"] == 1
    assert body["alignments"][0]["qseqid"] == "queryXml"
    assert body["alignments"][0]["sseqid"] == "XML123.1"
    assert body["alignments"][0]["stitle"] == "XML subject"


def test_alignments_prefers_merged_results_and_ignores_runtime_files(patched_storage):
    patched_storage["blobs"] = [
        {"name": "job123/job-run/merged_results.out.gz", "size": 123},
        {"name": "job123/job-run/shard_00/batch_000-blastn-core_nt_shard_00.out.gz", "size": 456},
        {"name": "job123/job-run/shard_00/logs/BLAST_RUNTIME-000.out", "size": 20},
        {"name": "job123/job-run/shard_00/metadata/BLASTDB_LENGTH.out", "size": 20},
    ]
    patched_storage["content"] = {
        "job123/job-run/merged_results.out.gz": _OUTFMT6,
        "job123/job-run/shard_00/batch_000-blastn-core_nt_shard_00.out.gz": (
            "queryZ\tSHARD_ONLY\t100.0\t10\t0\t0\t1\t10\t1\t10\t1e-9\t50\n"
        ),
        "job123/job-run/shard_00/logs/BLAST_RUNTIME-000.out": "runtime log",
        "job123/job-run/shard_00/metadata/BLASTDB_LENGTH.out": "1041443571674",
    }
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["blob_names"] == ["job123/job-run/merged_results.out.gz"]
    assert body["files_parsed"] == 1
    assert body["total_files"] == 1
    assert body["truncated"] is False
    assert body["total_hits"] == 3
    assert {hit["sseqid"] for hit in body["alignments"]} == {"NC_001", "NC_002"}


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


def test_alignments_reads_all_result_blobs_by_default(patched_storage):
    patched_storage["blobs"] = [
        {"name": "job123/part-001.out", "size": 10},
        {"name": "job123/part-002.out", "size": 10},
    ]
    patched_storage["content"] = {
        "job123/part-001.out": "queryA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\n",
        "job123/part-002.out": "queryC\tNC_003\t91.0\t90\t8\t1\t5\t94\t20\t109\t1e-20\t120\n",
    }
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_hits"] == 2
    assert body["filtered_hits"] == 2
    assert body["files_parsed"] == 2
    assert body["total_files"] == 2
    assert body["blob_name"] == ""
    assert body["blob_names"] == ["job123/part-001.out", "job123/part-002.out"]
    assert {hit["qseqid"] for hit in body["alignments"]} == {"queryA", "queryC"}


def test_alignments_sort_and_page_hits(patched_storage):
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={
            "storage_account": "stelb",
            "sort_by": "bitscore",
            "sort_dir": "desc",
            "page_size": 1,
            "page": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 1
    assert len(body["alignments"]) == body["returned"]
    assert len(body["alignments"]) <= body["page_size"]
    assert body["pages"] == 3
    assert body["alignments"][0]["qseqid"] == "queryB"


def test_alignments_default_sort_uses_ncbi_style_relevance_tiebreakers(patched_storage):
    patched_storage["content"] = (
        "queryA\tNC_LOW\t99.0\t100\t1\t0\t1\t100\t1\t100\t0.0\t90\n"
        "queryA\tNC_TIE_LOW_TOTAL\t99.0\t100\t1\t0\t1\t100\t1\t100\t0.0\t100\n"
        "queryA\tNC_TIE_HIGH_TOTAL\t99.0\t100\t1\t0\t1\t100\t1\t100\t0.0\t100\n"
        "queryA\tNC_TIE_HIGH_TOTAL\t98.0\t90\t2\t0\t1\t90\t110\t199\t0.0\t80\n"
        "queryA\tNC_HIGH\t99.0\t100\t1\t0\t1\t100\t1\t100\t0.0\t110\n"
    )
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "page_size": 10},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["sort_by"] == "relevance"
    assert [hit["sseqid"] for hit in body["alignments"]] == [
        "NC_HIGH",
        "NC_TIE_HIGH_TOTAL",
        "NC_TIE_HIGH_TOTAL",
        "NC_TIE_LOW_TOTAL",
        "NC_LOW",
    ]


def test_alignments_adds_query_coverage_and_review_status(patched_storage):
    patched_storage["content"] = (
        "# Fields: query id, subject id, % identity, alignment length, mismatches, "
        "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
        "query length, subject length, subject title\n"
        "assay1\tNC_001\t100.0\t100\t0\t0\t1\t100\t1\t100\t1e-80\t400\t100\t200\tTarget organism\n"
    )
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "min_query_cover": 95},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filtered_hits"] == 1
    hit = body["alignments"][0]
    assert hit["qcovs"] == 100.0
    assert hit["scovs"] == 50.0
    assert hit["review_status"] == "strong_match"
    assert hit["source_blob"] == "job123/results.out"


def test_alignments_query_coverage_uses_coordinate_span(patched_storage):
    patched_storage["content"] = (
        "# Fields: query id, subject id, % identity, alignment length, mismatches, "
        "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
        "query length, subject length, subject title\n"
        "assay1\tNC_001\t100.0\t120\t0\t0\t1\t80\t1\t80\t1e-80\t400\t100\t200\tTarget organism\n"
    )
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    hit = response.json()["alignments"][0]
    assert hit["qcovs"] == 80.0
    assert hit["scovs"] == 40.0
    assert hit["review_status"] == "review_priority"


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


def test_export_csv_converts_xml_to_rows(patched_storage):
    patched_storage["blobs"] = [{"name": "job123/merged_results.out.gz", "size": 123}]
    patched_storage["content"] = _OUTFMT5_XML
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "csv"},
    )
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["qseqid"] == "queryXml"
    assert rows[0]["sseqid"] == "XML123.1"
    assert rows[0]["stitle"] == "XML subject"
    assert rows[0]["qcovs"] == "100.0"
    assert rows[0]["review_status"] == "review_priority"
    assert rows[0]["source_blob"] == "job123/merged_results.out.gz"


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

    monkeypatch.setattr(storage_data, "read_result_blob_text", boom)
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

    monkeypatch.setattr(storage_data, "read_result_blob_text", boom)
    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/export",
        params={"storage_account": "stelb", "format": "csv"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "all_reads_failed"


# ---------------------------------------------------------------------------
# Round-2 hardening: subject_aggregates on /results/alignments and the new
# /results/taxonomy endpoint (server-side organism rollup that spans the
# full filtered set instead of the visible page).
# ---------------------------------------------------------------------------


def test_alignments_returns_subject_aggregates_with_max_total_and_hsp_count(
    patched_storage,
):
    """`/results/alignments` must include `subject_aggregates` so the SPA
    can render the NCBI "Max / Total" cell across the entire filtered
    result set without recomputing per-page on the client."""
    patched_storage["content"] = (
        # Two HSPs on the same subject NC_001 (sum 289 + 200 = 489) and
        # one HSP on NC_002 — exercises both the multi-HSP path and the
        # single-HSP path.
        "queryA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\n"
        "queryA\tNC_001\t96.0\t120\t4\t0\t1\t120\t260\t379\t2e-40\t200\n"
        "queryA\tNC_002\t95.0\t148\t7\t1\t1\t148\t200\t347\t2e-40\t250\n"
    )
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    aggregates = {row["sseqid"]: row for row in response.json()["subject_aggregates"]}
    assert set(aggregates) == {"NC_001", "NC_002"}
    nc1 = aggregates["NC_001"]
    assert nc1["max_bitscore"] == 289.0
    assert nc1["total_bitscore"] == 489.0
    assert nc1["hsp_count"] == 2
    nc2 = aggregates["NC_002"]
    assert nc2["max_bitscore"] == 250.0
    assert nc2["total_bitscore"] == 250.0
    assert nc2["hsp_count"] == 1


def test_alignments_subject_aggregates_respect_filters(patched_storage):
    """The aggregate spans the *filtered* set, not the unfiltered set —
    confirming a min_identity filter drops weaker HSPs from the rollup."""
    patched_storage["content"] = (
        "queryA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\n"
        "queryA\tNC_001\t40.0\t120\t60\t0\t1\t120\t260\t379\t2e-3\t40\n"
    )
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/alignments",
        params={"storage_account": "stelb", "min_identity": 50},
    )
    aggregates = {row["sseqid"]: row for row in response.json()["subject_aggregates"]}
    # Only the strong HSP survives the min_identity=50 filter; aggregate
    # should reflect that, NOT the unfiltered total.
    assert aggregates["NC_001"]["hsp_count"] == 1
    assert aggregates["NC_001"]["total_bitscore"] == 289.0


def test_taxonomy_returns_per_organism_rollup_with_filters(patched_storage):
    """`/results/taxonomy` rolls up by sscinames, honours filters, and
    includes `best_evalue` / `top_bitscore` per organism."""
    patched_storage["content"] = (
        "# Fields: query id, subject id, % identity, alignment length, mismatches, "
        "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
        "subject sci name\n"
        "qA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\tMonkeypox virus\n"
        "qA\tNC_002\t96.0\t150\t6\t0\t1\t150\t200\t349\t1e-40\t250\tMonkeypox virus\n"
        "qA\tNC_009\t40.0\t120\t60\t0\t1\t120\t260\t379\t2e-3\t40\tVaccinia virus\n"
    )
    from api.main import app

    client = TestClient(app)
    # Default max_evalue=10 keeps the Vaccinia hit; the Monkeypox group
    # has 2 hits, Vaccinia 1.
    response = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    rows = {row["organism"]: row for row in body["organisms"]}
    assert rows["Monkeypox virus"]["count"] == 2
    assert rows["Monkeypox virus"]["best_evalue"] == 1e-50
    assert rows["Monkeypox virus"]["top_bitscore"] == 289.0
    assert rows["Vaccinia virus"]["count"] == 1
    # Rows are sorted by count desc.
    assert body["organisms"][0]["organism"] == "Monkeypox virus"

    # Re-narrow with an organism filter — Vaccinia should drop out.
    response2 = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={"storage_account": "stelb", "organism": "Monkeypox"},
    )
    body2 = response2.json()
    assert {row["organism"] for row in body2["organisms"]} == {"Monkeypox virus"}
    assert body2["filtered_hits"] == 2


def test_taxonomy_returns_empty_when_no_blobs(monkeypatch, patched_storage):
    """When no result blobs exist the endpoint should respond with a
    structured empty payload (not 5xx)."""
    patched_storage["blobs"] = []
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/empty/results/taxonomy",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["organisms"] == []
    assert body["files_parsed"] == 0
    assert body["total_files"] == 0


def test_taxonomy_handles_unclassified_when_metadata_missing(patched_storage):
    """When sscinames/staxids are absent the rollup keys an "unclassified"
    bucket so the UI still shows hit counts."""
    # outfmt 6 default (12 columns, no sscinames) — every hit lands in
    # the unclassified bucket.
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={"storage_account": "stelb"},
    )
    body = response.json()
    assert len(body["organisms"]) == 1
    only = body["organisms"][0]
    assert only["organism"] == ""
    assert only["taxid"] == ""
    assert only["key"] == "unclassified"
    assert only["count"] == 3  # default _OUTFMT6 has 3 rows


def test_taxonomy_degraded_when_all_reads_fail(monkeypatch, patched_storage):
    """If every blob fails to download, surface `degraded` like the
    aggregate/alignments endpoints do — the SPA reuses the same banner."""
    from api.services import storage_data

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(storage_data, "read_result_blob_text", boom)
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={"storage_account": "stelb"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "all_reads_failed"
    assert body["organisms"] == []


# ---------------------------------------------------------------------------
# Round-3 hardening: include_lineage=true on /results/taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_include_lineage_calls_fetch_taxonomy_detail_per_taxid(
    monkeypatch, patched_storage
):
    """When `include_lineage=true` the endpoint should call
    `fetch_taxonomy_detail` once per distinct taxid in the top-N rows
    and stitch the parsed `lineage_ex` into each row."""
    patched_storage["content"] = (
        "# Fields: query id, subject id, % identity, alignment length, mismatches, "
        "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
        "subject sci name, subject taxids\n"
        "qA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\tMonkeypox virus\t10244\n"
        "qA\tNC_002\t96.0\t148\t6\t0\t1\t148\t200\t347\t1e-40\t250\tVaccinia virus\t10245\n"
    )
    from api.services import taxonomy as taxonomy_service

    calls: list[int] = []

    def fake_detail(taxid: int) -> dict:
        calls.append(taxid)
        return {
            "taxid": taxid,
            "scientific_name": f"taxid:{taxid}",
            "lineage": "Viruses; Monodnaviria; Heunggongvirae",
            "lineage_ex": [
                {"rank": "superkingdom", "taxid": 10239, "scientific_name": "Viruses"},
                {"rank": "genus", "taxid": 10240, "scientific_name": "Orthopoxvirus"},
            ],
        }

    monkeypatch.setattr(taxonomy_service, "fetch_taxonomy_detail", fake_detail)
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={"storage_account": "stelb", "include_lineage": "true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(calls) == [10244, 10245]
    # Each organism row carries the parsed lineage_ex from the mocked call.
    rows = body["organisms"]
    assert all("lineage" in row for row in rows)
    assert all(
        any(step["scientific_name"] == "Orthopoxvirus" for step in row["lineage_ex"])
        for row in rows
    )
    meta = body["lineage"]
    assert meta["requested"] is True
    assert meta["looked_up"] == 2
    assert meta["failed"] == 0


def test_taxonomy_include_lineage_tolerates_eutils_failure(monkeypatch, patched_storage):
    """If NCBI eutils raises, the taxonomy endpoint still returns the
    organism rollup with `lineage.failed` counted; the row stays in
    the response WITHOUT the `lineage_ex` field."""
    patched_storage["content"] = (
        "# Fields: query id, subject id, % identity, alignment length, mismatches, "
        "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
        "subject sci name, subject taxids\n"
        "qA\tNC_001\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-50\t289\tMonkeypox virus\t10244\n"
    )
    from api.services import taxonomy as taxonomy_service

    def boom(_taxid: int) -> dict:
        raise taxonomy_service.TaxonomySearchUnavailable("eutils down")

    monkeypatch.setattr(taxonomy_service, "fetch_taxonomy_detail", boom)
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={"storage_account": "stelb", "include_lineage": "true"},
    )
    assert response.status_code == 200
    body = response.json()
    # Rollup itself still works.
    assert body["organisms"][0]["organism"] == "Monkeypox virus"
    assert body["organisms"][0].get("lineage_ex") is None
    meta = body["lineage"]
    assert meta["requested"] is True
    assert meta["looked_up"] == 0
    assert meta["failed"] == 1


def test_taxonomy_include_lineage_respects_taxid_limit(monkeypatch, patched_storage):
    """`lineage_taxid_limit` caps how many distinct taxids are looked up
    (NCBI eutils is rate-limited; the cap is the rate-limit defence)."""
    rows = []
    for index in range(5):
        rows.append(
            "# Fields: query id, subject id, % identity, alignment length, mismatches, "
            "gap opens, q. start, q. end, s. start, s. end, evalue, bit score, "
            "subject sci name, subject taxids\n"
            if index == 0
            else ""
        )
        rows.append(
            f"qA\tNC_{index:03d}\t99.5\t150\t1\t0\t1\t150\t100\t249\t1e-{index + 50}\t{289 - index}"
            f"\tspecies{index}\t{10000 + index}\n"
        )
    patched_storage["content"] = "".join(rows)
    from api.services import taxonomy as taxonomy_service

    calls: list[int] = []

    def fake_detail(taxid: int) -> dict:
        calls.append(taxid)
        return {
            "taxid": taxid,
            "scientific_name": f"taxid:{taxid}",
            "lineage": "",
            "lineage_ex": [],
        }

    monkeypatch.setattr(taxonomy_service, "fetch_taxonomy_detail", fake_detail)
    from api.main import app

    client = TestClient(app)
    response = client.get(
        "/api/blast/jobs/job123/results/taxonomy",
        params={
            "storage_account": "stelb",
            "include_lineage": "true",
            "lineage_taxid_limit": 2,
        },
    )
    body = response.json()
    # Only the top 2 (by hit count desc, then by ordering) get looked up.
    assert len(calls) == 2
    assert body["lineage"]["limit_reached"] >= 1
