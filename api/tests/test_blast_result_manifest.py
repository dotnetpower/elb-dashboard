from __future__ import annotations

from api.services.blast_result_manifest import build_result_manifest


def test_result_manifest_classifies_parseable_result_files() -> None:
    manifest = build_result_manifest(
        job_id="job-1",
        files=[
            {"name": "job-1/results.xml", "size": 123},
            {"name": "job-1/provenance.json", "size": 45},
        ],
    )

    assert manifest["status"] == "available"
    assert manifest["file_count"] == 2
    assert manifest["parseable_count"] == 1
    assert manifest["files"][0]["format"] == "blast_xml"


def test_result_manifest_distinguishes_no_result_files_from_degraded() -> None:
    empty = build_result_manifest(job_id="job-1", files=[])
    degraded = build_result_manifest(
        job_id="job-1",
        files=[],
        degraded_reason="storage_unreachable",
    )

    assert empty["status"] == "no_result_files"
    assert degraded["status"] == "degraded"
    assert degraded["degraded_reason"] == "storage_unreachable"
