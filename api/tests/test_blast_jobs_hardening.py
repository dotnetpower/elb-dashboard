"""Tests for BLAST job route hardening helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes import blast_jobs


def test_normalise_job_registry_state_accepts_list_and_dict() -> None:
    jobs = [{"job_id": "job-1"}, "bad", {"job_id": "job-2"}]

    assert blast_jobs._normalise_job_registry_state(jobs) == [
        {"job_id": "job-1"},
        {"job_id": "job-2"},
    ]
    assert blast_jobs._normalise_job_registry_state({"jobs": jobs}) == [
        {"job_id": "job-1"},
        {"job_id": "job-2"},
    ]


def test_validate_job_id_rejects_path_and_shell_metacharacters() -> None:
    assert blast_jobs._validate_job_id_param("job-5039e9f4e75f") == "job-5039e9f4e75f"
    assert blast_jobs._validate_job_id_param("../job") is None
    assert blast_jobs._validate_job_id_param("job/other") is None
    assert blast_jobs._validate_job_id_param("job;rm") is None
    assert blast_jobs._validate_job_id_param("x" * 201) is None


def test_result_blob_must_stay_under_job_prefix() -> None:
    assert blast_jobs._validate_result_blob_name("job-1/out/result.out", "job-1") is None
    assert "prefix" in blast_jobs._validate_result_blob_name("job-2/out/result.out", "job-1")
    assert (
        blast_jobs._validate_result_blob_name("../job-1/result.out", "job-1")
        == "invalid blob_name"
    )
    assert (
        blast_jobs._validate_result_blob_name("/job-1/result.out", "job-1")
        == "invalid blob_name"
    )
