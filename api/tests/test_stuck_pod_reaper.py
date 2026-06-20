"""Tests for the pure stuck-pod reaping decision.

Responsibility: Lock the safety contract of `classify_stuck_blast_pod` — above
all, a Running / starting / completed pod is NEVER reaped, and only an
explicitly-wedged pod past its age threshold is.
Edit boundaries: Pure-function tests; no K8s / network.
Key entry points: the test functions below.
Risky contracts: If any "Running -> keep" case ever flips to "reap", reaping
could terminate in-progress BLAST work — that must fail the suite loudly.
Validation: `uv run pytest -q api/tests/test_stuck_pod_reaper.py`.
"""

from __future__ import annotations

import pytest
from api.services.k8s.stuck_pod_reaper import (
    ReaperThresholds,
    classify_stuck_blast_pod,
)

_T = ReaperThresholds(pending_seconds=900, waiting_seconds=900)


@pytest.mark.parametrize(
    "status",
    [
        "Running",
        "Completed",
        "Succeeded",
        "Terminating",
        "ContainerCreating",
        "PodInitializing",
        "Unknown",
        "",
    ],
)
def test_progressing_pod_is_never_reaped_even_when_old(status: str) -> None:
    # 30 days old — age must NEVER override the progress allowlist.
    assert (
        classify_stuck_blast_pod(
            display_status=status, age_seconds=30 * 86400, thresholds=_T
        )
        == "keep"
    )


def test_running_is_kept_regardless_of_age() -> None:
    for age in (0, 60, 900, 10_000, 10**9):
        assert (
            classify_stuck_blast_pod(
                display_status="Running", age_seconds=age, thresholds=_T
            )
            == "keep"
        )


@pytest.mark.parametrize(
    "status",
    ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName"],
)
def test_wedged_waiting_reason_reaped_only_past_threshold(status: str) -> None:
    assert (
        classify_stuck_blast_pod(display_status=status, age_seconds=899, thresholds=_T)
        == "keep"
    )
    assert (
        classify_stuck_blast_pod(display_status=status, age_seconds=900, thresholds=_T)
        == "reap"
    )


def test_init_prefixed_waiting_reason_is_caught() -> None:
    assert (
        classify_stuck_blast_pod(
            display_status="Init:CrashLoopBackOff", age_seconds=1000, thresholds=_T
        )
        == "reap"
    )
    assert (
        classify_stuck_blast_pod(
            display_status="Init:ImagePullBackOff", age_seconds=100, thresholds=_T
        )
        == "keep"
    )


def test_pending_reaped_only_past_threshold() -> None:
    assert (
        classify_stuck_blast_pod(
            display_status="Pending", age_seconds=899, thresholds=_T
        )
        == "keep"
    )
    assert (
        classify_stuck_blast_pod(
            display_status="Pending", age_seconds=900, thresholds=_T
        )
        == "reap"
    )


@pytest.mark.parametrize("status", ["Error", "ExitCode:1", "Signal:9", "ExitCode:137"])
def test_terminated_error_is_not_reaped(status: str) -> None:
    # A one-shot Error / OOMKill is left to the Job's backoffLimit + the
    # activeDeadlineSeconds backstop; the reaper only targets non-resolving
    # waiting states and unschedulable Pending, not terminated retries.
    assert (
        classify_stuck_blast_pod(
            display_status=status, age_seconds=10_000, thresholds=_T
        )
        == "keep"
    )
