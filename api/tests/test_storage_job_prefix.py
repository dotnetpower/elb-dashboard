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
    build_dated_results_prefix,
    date_layout_enabled,
    default_results_prefix,
    elastic_blast_subdir_prefix,
    normalize_results_prefix,
    resolve_results_prefix,
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


# --- date layout (issue #67) ----------------------------------------------


def test_date_layout_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DATE_LAYOUT_ENABLED", raising=False)
    assert date_layout_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "On", "yes"])
def test_date_layout_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", value)
    assert date_layout_enabled() is True


def test_build_dated_results_prefix() -> None:
    from datetime import UTC, datetime

    fixed = datetime(2026, 6, 23, 9, 0, 0, tzinfo=UTC)
    assert build_dated_results_prefix("job-x", now=fixed) == "2026/06/23/job-x/"


def test_resolve_uses_explicit_state_without_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with the flag on, an explicit state short-circuits the Table lookup.
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    state = JobState(
        job_id="job-1", type="blast", status="completed", results_prefix="2026/06/23/job-1/"
    )

    class _Repo:
        def get(self, _job_id: str) -> None:  # pragma: no cover - must not be called
            raise AssertionError("lookup must be skipped when state is provided")

    assert resolve_results_prefix("job-1", state=state, repo=_Repo()) == "2026/06/23/job-1/"


def test_resolve_flag_off_skips_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_DATE_LAYOUT_ENABLED", raising=False)

    class _Repo:
        def get(self, _job_id: str) -> None:  # pragma: no cover - must not be called
            raise AssertionError("no Table lookup when date layout is OFF")

    assert resolve_results_prefix("job-2", repo=_Repo()) == "job-2/"


def test_resolve_flag_on_looks_up_dated_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    dated = JobState(
        job_id="job-3", type="blast", status="completed", results_prefix="2026/06/23/job-3/"
    )

    class _Repo:
        def get(self, job_id: str) -> JobState:
            assert job_id == "job-3"
            return dated

    assert resolve_results_prefix("job-3", repo=_Repo()) == "2026/06/23/job-3/"


def test_resolve_flag_on_legacy_flat_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    legacy = JobState(job_id="job-4", type="blast", status="completed")  # results_prefix None

    class _Repo:
        def get(self, _job_id: str) -> JobState:
            return legacy

    assert resolve_results_prefix("job-4", repo=_Repo()) == "job-4/"


def test_resolve_lookup_failure_degrades_to_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")

    class _Repo:
        def get(self, _job_id: str) -> JobState:
            raise RuntimeError("table unreachable")

    # A resolver must never raise into a listing/streaming path.
    assert resolve_results_prefix("job-5", repo=_Repo()) == "job-5/"


def test_results_job_url_flat_when_date_layout_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # The critical no-op guarantee: with the flag OFF, the elastic-blast results
    # bucket is byte-identical to the legacy flat layout.
    monkeypatch.delenv("STORAGE_DATE_LAYOUT_ENABLED", raising=False)
    from api.services.blast.task_config import results_job_url

    assert (
        results_job_url("elbstg01", "job-9")
        == "https://elbstg01.blob.core.windows.net/results/job-9"
    )


def test_results_job_url_dated_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag ON + a stored dated row → the elastic-blast results bucket is dated,
    # matching the stored results_prefix exactly (no midnight-boundary drift).
    monkeypatch.setenv("STORAGE_DATE_LAYOUT_ENABLED", "true")
    dated = JobState(
        job_id="job-9", type="blast", status="queued", results_prefix="2026/06/23/job-9/"
    )

    class _Repo:
        def get(self, _job_id: str) -> JobState:
            return dated

    monkeypatch.setattr("api.services.state_repo.get_state_repo", lambda: _Repo())
    from api.services.blast.task_config import results_job_url

    assert (
        results_job_url("elbstg01", "job-9")
        == "https://elbstg01.blob.core.windows.net/results/2026/06/23/job-9"
    )


