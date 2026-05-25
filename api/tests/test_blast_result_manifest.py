"""Tests for BLAST Result Manifest behavior.

Responsibility: Tests for BLAST Result Manifest behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_result_manifest_classifies_parseable_result_files`,
`test_result_manifest_distinguishes_no_result_files_from_degraded`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_blast_result_manifest.py`.
"""

from __future__ import annotations

from api.services.blast.result_manifest import build_result_manifest


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
    assert manifest["files"][0]["filename"] == "results.xml"
    assert manifest["files"][0]["size_bytes"] == 123


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
