"""Tests for DB order-oracle runtime classification helpers.

Responsibility: Verify retry-aware Job aggregation and exact/non-empty part
    validation with in-memory observations.
Edit boundaries: Pure/fake runtime inputs only; task state transitions belong
    to `test_oracle_task.py`.
Key entry points: `test_job_classification_waits_for_retrying_job`,
    `test_part_validation_requires_exact_nonempty_files`.
Risky contracts: A failed pod count without a terminal Failed condition must
    not fail the run, and extra/missing/zero-byte parts must block publication.
Validation: `uv run pytest -q api/tests/test_oracle_runtime.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.services.db.oracle_runtime import (
    classify_oracle_jobs,
    validate_oracle_parts,
)


class _Container:
    def __init__(self, rows: dict[str, int]) -> None:
        self.rows = rows

    def list_blobs(self, *, name_starts_with: str):
        return [
            SimpleNamespace(name=name, size=size)
            for name, size in self.rows.items()
            if name.startswith(name_starts_with)
        ]


def test_job_classification_waits_for_retrying_job() -> None:
    progress = classify_oracle_jobs(
        ["oracle-00", "oracle-01"],
        [
            {"name": "oracle-00", "status": "Complete", "succeeded": 1},
            {"name": "oracle-01", "status": "Pending", "failed": 1},
        ],
    )

    assert progress.status == "running"
    assert progress.complete == ("oracle-00",)
    assert progress.failed == ()


def test_job_classification_reports_terminal_failure_and_missing() -> None:
    progress = classify_oracle_jobs(
        ["oracle-00", "oracle-01", "oracle-02"],
        [
            {"name": "oracle-00", "status": "Complete"},
            {"name": "oracle-01", "status": "Failed"},
        ],
    )

    assert progress.status == "failed"
    assert progress.failed == ("oracle-01",)
    assert progress.missing == ("oracle-02",)


def test_part_validation_requires_exact_nonempty_files() -> None:
    prefix = "metadata/oracles/core_nt/parts/run-1/"
    expected = [f"{prefix}00.txt", f"{prefix}01.txt"]

    ready = validate_oracle_parts(
        _Container({expected[0]: 12, expected[1]: 34}),
        expected_paths=expected,
        part_prefix=prefix,
    )
    broken = validate_oracle_parts(
        _Container({expected[0]: 0, f"{prefix}02.txt": 10}),
        expected_paths=expected,
        part_prefix=prefix,
    )

    assert ready == {
        "ready": True,
        "ready_parts": 2,
        "expected_parts": 2,
        "missing": [],
        "unexpected": [],
        "empty": [],
    }
    assert broken["ready"] is False
    assert broken["missing"] == [expected[1]]
    assert broken["empty"] == [expected[0]]
    assert broken["unexpected"] == [f"{prefix}02.txt"]
