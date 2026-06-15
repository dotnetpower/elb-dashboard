"""Tests for the durable BLAST SUCCESS-marker ground-truth helper.

Responsibility: Lock in ``has_blast_success_marker`` — the authoritative
"this job completed" signal the stale-job reconciler consults before
declaring an unreachable, quiet job ``worker_lost``. The marker
(``.../metadata/SUCCESS.txt``) is written last by the cluster-side finalizer
and survives AKS teardown, so its presence must map to True and its absence /
any Storage error must map to False (fail-safe: never falsely complete a job).
Edit boundaries: Exercise only the marker helper's blob-name matching and
best-effort guards; storage_data + get_credential are monkeypatched.
Key entry points: ``test_*``.
Risky contracts: Best-effort — a False on Storage failure keeps the legacy
``worker_lost`` path, so a transient hiccup never falsely completes a job.
Validation: ``uv run pytest -q api/tests/test_blast_success_marker.py``.
"""

from __future__ import annotations

import pytest
from api.services.blast import result_analytics


def _patch_blobs(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    monkeypatch.setattr(result_analytics, "get_credential", lambda: object())
    monkeypatch.setattr(
        result_analytics.storage_data,
        "list_result_blobs",
        lambda *_args, **_kwargs: [{"name": n} for n in names],
    )


def test_success_marker_present_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_blobs(
        monkeypatch,
        [
            "job-1/job-elastic/batch_000.out.gz",
            "job-1/job-elastic/metadata/SUCCESS.txt",
        ],
    )
    assert result_analytics.has_blast_success_marker("stelb", "job-1") is True


def test_success_marker_absent_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_blobs(
        monkeypatch,
        [
            "job-1/job-elastic/batch_000.out.gz",
            "job-1/job-elastic/metadata/FAILURE.txt",
        ],
    )
    assert result_analytics.has_blast_success_marker("stelb", "job-1") is False


def test_success_marker_empty_inputs_return_false() -> None:
    assert result_analytics.has_blast_success_marker("", "job-1") is False
    assert result_analytics.has_blast_success_marker("stelb", "") is False


def test_success_marker_storage_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(result_analytics, "get_credential", lambda: object())

    def _raise(*_args, **_kwargs):
        raise RuntimeError("storage unreachable")

    monkeypatch.setattr(result_analytics.storage_data, "list_result_blobs", _raise)
    # Fail-safe: a Storage hiccup must never falsely complete a job.
    assert result_analytics.has_blast_success_marker("stelb", "job-1") is False
