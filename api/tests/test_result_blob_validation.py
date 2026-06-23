"""Tests for layout-aware result-blob ownership validation (issue #70).

Responsibility: Prove ``validate_result_blob_name`` / ``blob_belongs_to_job``
accept a job's own result blobs in BOTH the flat (``{job_id}/file``) and the
date-tiered (``YYYY/MM/DD/{job_id}/file``) layouts, stay as tight as the legacy
check for flat blobs (reject ``other/{job_id}/x``), and reject cross-job,
traversal, encoding-trick, bare-directory, and empty-segment names.
Edit boundaries: Pure validation behaviour. No Azure network.
Key entry points: ``test_belongs_*``, ``test_validate_*``.
Risky contracts: the date-prefix allowance is exactly ``YYYY/MM/DD`` — a looser
match would let an arbitrary prefix smuggle a blob into a job's namespace.
Validation: ``uv run pytest -q api/tests/test_result_blob_validation.py``.
"""

from __future__ import annotations

import pytest
from api.services.blast.result_analytics import (
    InvalidResultBlobName,
    blob_belongs_to_job,
    validate_result_blob_name,
)


@pytest.mark.parametrize(
    "blob_name",
    [
        "job-1/result.out",
        "job-1/metadata/SUCCESS.txt",
        "2026/06/23/job-1/result.out",
        "2026/06/23/job-1/metadata/SUCCESS.txt",
    ],
)
def test_belongs_accepts_flat_and_dated(blob_name: str) -> None:
    assert blob_belongs_to_job(blob_name, "job-1") is True


@pytest.mark.parametrize(
    "blob_name",
    [
        "job-2/result.out",  # different job, flat
        "2026/06/23/job-2/result.out",  # different job, dated
        "other/job-1/result.out",  # non-date prefix → not this job's dir
        "evil/2026/06/23/job-1/result.out",  # extra non-date head segment
        "2026/06/job-1/result.out",  # partial (2-segment) date head → reject
        "2026/06/23/45/job-1/result.out",  # 4-segment head → not a YYYY/MM/DD
        "job-1",  # bare directory, no file
        "job-1/",  # trailing slash → empty tail
        "job-1//result.out",  # empty segment
        "prefix-job-1/result.out",  # substring, not a segment
    ],
)
def test_belongs_rejects(blob_name: str) -> None:
    assert blob_belongs_to_job(blob_name, "job-1") is False


def test_validate_accepts_dated() -> None:
    validate_result_blob_name("2026/06/23/job-1/result.out", "job-1")  # no raise


def test_validate_accepts_flat() -> None:
    validate_result_blob_name("job-1/result.out", "job-1")  # no raise


@pytest.mark.parametrize(
    "blob_name",
    [
        "job-2/result.out",
        "other/job-1/result.out",
        "job-1/../job-2/secret",  # traversal
        "job-1/result.out?sas=x",  # query smuggling
        "job-1/result.out#frag",  # fragment
        "job-1\\result.out",  # backslash separator
        "job-1/%2e%2e/secret",  # encoded traversal
        "/job-1/result.out",  # leading slash
    ],
)
def test_validate_rejects(blob_name: str) -> None:
    with pytest.raises(InvalidResultBlobName):
        validate_result_blob_name(blob_name, "job-1")


def test_validate_rejects_bad_job_id() -> None:
    with pytest.raises(InvalidResultBlobName):
        validate_result_blob_name("job-1/r.out", "bad/id")
    with pytest.raises(InvalidResultBlobName):
        validate_result_blob_name("job-1/r.out", "")
