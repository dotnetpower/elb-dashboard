"""Unit tests for the canonical job-prefix resolver + JobState round-trip.

Responsibility: Prove ``api.services.storage.job_prefix`` always emits a
collision-free single-trailing-slash prefix, honours a stored
``JobState.results_prefix``, falls back to ``{job_id}/`` for legacy rows, and
that ``JobState`` persists/reads back ``results_prefix`` (defaulting to the flat
layout) without disturbing the other columns.
Edit boundaries: Pure resolver + dataclass serialization behaviour. No Azure
network / Table I/O.
Key entry points: ``test_normalize_*``, ``test_results_prefix_from_state_*``,
``test_jobstate_roundtrip_*``.
Risky contracts: The default written by ``to_entity`` MUST stay ``{job_id}/`` so
#66 is a behaviour-preserving refactor; #67 overrides it explicitly.
Validation: ``uv run pytest -q api/tests/test_storage_job_prefix.py``.
"""

from __future__ import annotations

import pytest
from api.services.state.job_state import JobState
from api.services.storage.job_prefix import (
    default_results_prefix,
    elastic_blast_subdir_prefix,
    normalize_results_prefix,
    results_prefix_from_state,
)


@pytest.mark.parametrize(
    ("prefix", "job_id", "expected"),
    [
        ("", "job-abc", "job-abc/"),
        (None, "job-abc", "job-abc/"),
        ("   ", "job-abc", "job-abc/"),
        ("/", "job-abc", "job-abc/"),
        ("job-abc", "job-abc", "job-abc/"),
        ("job-abc/", "job-abc", "job-abc/"),
        ("/job-abc/", "job-abc", "job-abc/"),
        ("2026/06/23/job-abc", "job-abc", "2026/06/23/job-abc/"),
        ("2026/06/23/job-abc/", "job-abc", "2026/06/23/job-abc/"),
        # Traversal in the stored value falls back to the safe job id.
        ("../evil", "job-abc", "job-abc/"),
        ("a/../b", "job-abc", "job-abc/"),
    ],
)
def test_normalize_results_prefix(prefix: str | None, job_id: str, expected: str) -> None:
    assert normalize_results_prefix(prefix, job_id) == expected


def test_default_results_prefix_is_flat_layout() -> None:
    assert default_results_prefix("job-xyz") == "job-xyz/"


def test_elastic_blast_subdir_prefix() -> None:
    assert elastic_blast_subdir_prefix("job-xyz/") == "job-xyz/job-"
    assert elastic_blast_subdir_prefix("2026/06/23/job-xyz/") == "2026/06/23/job-xyz/job-"


def test_results_prefix_from_state_uses_stored_value() -> None:
    state = JobState(
        job_id="job-1", type="blast", status="completed", results_prefix="2026/06/23/job-1/"
    )
    assert results_prefix_from_state(state) == "2026/06/23/job-1/"


def test_results_prefix_from_state_falls_back_for_legacy_row() -> None:
    # Legacy row persisted before the column existed → results_prefix is None.
    state = JobState(job_id="job-2", type="blast", status="completed")
    assert results_prefix_from_state(state) == "job-2/"


# --- JobState round-trip ---------------------------------------------------


def test_jobstate_to_entity_defaults_results_prefix_to_flat_layout() -> None:
    state = JobState(job_id="job-3", type="blast", status="queued")
    entity = state.to_entity()
    assert entity["results_prefix"] == "job-3/"


def test_jobstate_to_entity_preserves_explicit_results_prefix() -> None:
    state = JobState(
        job_id="job-4", type="blast", status="queued", results_prefix="2026/06/23/job-4/"
    )
    entity = state.to_entity()
    assert entity["results_prefix"] == "2026/06/23/job-4/"


def test_jobstate_from_entity_reads_results_prefix() -> None:
    entity = {
        "PartitionKey": "job-5",
        "RowKey": "current",
        "type": "blast",
        "status": "completed",
        "results_prefix": "2026/06/23/job-5/",
    }
    state = JobState.from_entity(entity)
    assert state.results_prefix == "2026/06/23/job-5/"


def test_jobstate_from_entity_missing_results_prefix_is_none() -> None:
    entity = {
        "PartitionKey": "job-6",
        "RowKey": "current",
        "type": "blast",
        "status": "completed",
    }
    state = JobState.from_entity(entity)
    assert state.results_prefix is None
    # Resolver still yields a safe prefix for the legacy row.
    assert results_prefix_from_state(state) == "job-6/"


def test_jobstate_roundtrip_results_prefix() -> None:
    state = JobState(
        job_id="job-7", type="blast", status="completed", results_prefix="2026/06/23/job-7/"
    )
    restored = JobState.from_entity(state.to_entity())
    assert restored.results_prefix == "2026/06/23/job-7/"
