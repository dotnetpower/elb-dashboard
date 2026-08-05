"""Tests for the per-job result-read byte budget.

Responsibility: Pin the memory ceiling on `read_result_blob_texts_parallel`.
    The reader returns a LIST, so every decoded blob is alive at once — without
    a budget the worst case is RESULTS_MAX_FILES (20) x max_bytes (10-20 MB) =
    200-400 MB of text held simultaneously in a 2 GiB api sidecar. The 2026-06-21
    large-XML change note flagged this as needing "a per-job total-bytes budget
    rather than a blanket cap bump"; these tests are that budget's contract.
Edit boundaries: Exercises the reader with a stubbed blob-text function; no live
    Storage and no credential plumbing.
Key entry points: the `test_*` functions.
Risky contracts: the first blob must ALWAYS be read in full (a budget that can
    skip file #1 turns a normal export into an `all_reads_failed` 503), and a
    skipped blob must carry an error so callers cannot silently under-report.
Validation: ``uv run pytest -q api/tests/test_result_read_budget.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast import result_analytics as ra


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RESULTS_READ_TOTAL_MAX_BYTES", raising=False)
    monkeypatch.setattr(ra, "get_credential", lambda: object())
    yield


def _stub_reads(monkeypatch: pytest.MonkeyPatch, size: int) -> list[str]:
    """Make every blob read return ``size`` characters; record the paths read."""
    read_paths: list[str] = []

    def _fake(_cred: Any, _acct: str, _container: str, path: str, *, max_bytes: int) -> str:
        read_paths.append(path)
        return "x" * min(size, max_bytes)

    monkeypatch.setattr(ra.storage_data, "read_result_blob_text", _fake)
    return read_paths


def _blobs(count: int) -> list[dict[str, str]]:
    return [{"name": f"job/f{i}.out"} for i in range(count)]


def test_budget_stops_reading_and_flags_the_skipped_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_paths = _stub_reads(monkeypatch, size=100)

    results = ra.read_result_blob_texts_parallel(
        "acct",
        _blobs(12),
        max_bytes=100,
        max_workers=2,
        total_max_bytes=250,
    )

    assert len(results) == 12
    # Budget spent after the batch that crossed 250 bytes; the rest are skipped
    # WITHOUT being fetched — that is the whole point.
    assert len(read_paths) < 12
    skipped = [r for r in results if isinstance(r[2], ra.ResultReadBudgetExceeded)]
    assert skipped, "over-budget blobs must be reported, never silently dropped"
    assert all(r[1] is None for r in skipped)


def test_input_order_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers zip results back to their blob list positionally."""
    _stub_reads(monkeypatch, size=10)

    results = ra.read_result_blob_texts_parallel(
        "acct", _blobs(7), max_bytes=100, max_workers=3
    )

    assert [r[0] for r in results] == [f"job/f{i}.out" for i in range(7)]


def test_first_blob_is_always_read_even_with_a_tiny_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget that could skip file #1 would turn an export into a 503."""
    read_paths = _stub_reads(monkeypatch, size=1000)

    results = ra.read_result_blob_texts_parallel(
        "acct",
        _blobs(4),
        max_bytes=1000,
        max_workers=1,
        total_max_bytes=1,  # absurdly small on purpose
    )

    assert read_paths[:1] == ["job/f0.out"]
    assert results[0][1] is not None and results[0][2] is None


def test_budget_is_floored_at_one_full_file() -> None:
    assert ra._result_read_total_budget(1, 10 * 1024) == 10 * 1024
    assert ra._result_read_total_budget(None, 10 * 1024) >= 10 * 1024


def test_budget_env_override_and_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESULTS_READ_TOTAL_MAX_BYTES", str(32 * 1024 * 1024))
    assert ra._result_read_total_budget(None, 1024) == 32 * 1024 * 1024

    monkeypatch.setenv("RESULTS_READ_TOTAL_MAX_BYTES", "not-a-number")
    # A typo must not disable the ceiling — fall back to the default.
    assert ra._result_read_total_budget(None, 1024) == ra.RESULTS_READ_TOTAL_MAX_BYTES


def test_under_budget_jobs_are_completely_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case must behave exactly as before the budget existed."""
    read_paths = _stub_reads(monkeypatch, size=10)

    results = ra.read_result_blob_texts_parallel(
        "acct", _blobs(20), max_bytes=1024, max_workers=8
    )

    assert len(read_paths) == 20
    assert all(r[1] is not None and r[2] is None for r in results)


def test_empty_blob_names_still_yield_placeholder_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers skip on a falsy path; that contract predates the budget."""
    _stub_reads(monkeypatch, size=10)

    results = ra.read_result_blob_texts_parallel(
        "acct", [{"name": ""}, {"name": "job/f1.out"}], max_bytes=1024, max_workers=2
    )

    assert results[0] == ("", None, None)
    assert results[1][0] == "job/f1.out"
