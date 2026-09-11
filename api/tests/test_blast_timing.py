"""Tests for stable BLAST timing breakdowns.

Responsibility: Verify queue, processing, result-ready, delivery, and phase timing derivation.
Edit boundaries: Pure timing fixtures only; no Azure, Storage, Kubernetes, Redis, or HTTP calls.
Key entry points: pytest test functions
Risky contracts: Mutable job `updated_at` must never affect terminal timing, and missing evidence
  must remain nullable rather than being fabricated.
Validation: `uv run pytest -q api/tests/test_blast_timing.py`.
"""

from api.services.blast.timing import build_job_timing


def test_servicebus_timing_separates_queue_processing_and_delivery() -> None:
    timing = build_job_timing(
        {
            "status": "completed",
            "submission_source": "servicebus",
            "created_at": "2026-09-11T02:14:36+00:00",
            "started_at": "2026-09-11T02:16:15+00:00",
            "updated_at": "2026-09-11T03:30:31+00:00",
            "queue_wait_seconds": 86,
            "run_seconds": 138,
            "elapsed_seconds": 224,
        },
        {
            "stages": [
                {"stage": "enqueued", "ts": "2026-09-11T02:13:34+00:00"},
                {"stage": "completion_published", "ts": "2026-09-11T02:19:15+00:00"},
            ],
            "metrics": {"queue_dwell_ms": 62_000, "submit_latency_ms": 13_000},
        },
    )

    assert timing["service_bus_queue_seconds"] == 62
    assert timing["execution_queue_seconds"] == 86
    assert timing["total_queue_seconds"] == 148
    assert timing["submit_seconds"] == 13
    assert timing["processing_seconds"] == 138
    assert timing["time_to_result_seconds"] == 299
    assert timing["result_ready_at"] == "2026-09-11T02:18:33Z"
    assert timing["completion_published_at"] == "2026-09-11T02:19:15Z"
    assert timing["status_delivery_seconds"] == 42
    assert timing["unattributed_seconds"] == 0
    assert timing["breakdown_complete"] is True


def test_updated_at_never_changes_terminal_processing_time() -> None:
    base = {
        "status": "completed",
        "submission_source": "external_api",
        "started_at": "2026-09-11T02:16:15+00:00",
        "run_seconds": 138,
        "elapsed_seconds": 224,
    }

    early = build_job_timing({**base, "updated_at": "2026-09-11T02:18:34+00:00"})
    late = build_job_timing({**base, "updated_at": "2026-09-11T03:30:31+00:00"})

    assert early == late
    assert late["processing_seconds"] == 138
    assert late["time_to_result_seconds"] == 224


def test_missing_timing_evidence_stays_nullable() -> None:
    timing = build_job_timing(
        {
            "status": "completed",
            "submission_source": "servicebus",
            "created_at": "2026-09-11T02:14:36+00:00",
            "updated_at": "2026-09-11T03:30:31+00:00",
        }
    )

    assert timing["time_to_result_seconds"] is None
    assert timing["processing_seconds"] is None
    assert timing["result_ready_at"] is None
    assert timing["queue_complete"] is False
    assert timing["breakdown_complete"] is False


def test_servicebus_execution_elapsed_is_not_total_without_broker_boundaries() -> None:
    timing = build_job_timing(
        {
            "submission_source": "servicebus",
            "queue_wait_seconds": 86,
            "run_seconds": 138,
            "elapsed_seconds": 224,
        }
    )

    assert timing["execution_elapsed_seconds"] == 224
    assert timing["time_to_result_seconds"] is None
    assert timing["queue_complete"] is False
    assert timing["breakdown_complete"] is False


def test_timing_exposes_positive_gap_and_rejects_negative_accounting() -> None:
    positive_gap = build_job_timing(
        {
            "status": "completed",
            "submission_source": "external_api",
            "queue_wait_seconds": 10,
            "run_seconds": 20,
            "elapsed_seconds": 35,
        }
    )
    contradictory = build_job_timing(
        {
            "status": "completed",
            "submission_source": "external_api",
            "queue_wait_seconds": 20,
            "run_seconds": 20,
            "elapsed_seconds": 30,
        }
    )

    assert positive_gap["unattributed_seconds"] == 5
    assert positive_gap["breakdown_complete"] is True
    assert contradictory["unattributed_seconds"] is None
    assert contradictory["breakdown_complete"] is False


def test_active_elapsed_never_becomes_result_ready_or_time_to_result() -> None:
    timing = build_job_timing(
        {
            "status": "running",
            "submission_source": "external_api",
            "started_at": "2026-09-11T02:16:15+00:00",
            "queue_wait_seconds": 10,
            "run_seconds": 20,
            "elapsed_seconds": 30,
        }
    )

    assert timing["processing_seconds"] == 20
    assert timing["result_ready_at"] is None
    assert timing["time_to_result_seconds"] is None
    assert timing["breakdown_complete"] is False


def test_execution_phases_use_runtime_and_container_evidence() -> None:
    timing = build_job_timing(
        {
            "execution_timing": {
                "orchestration_seconds": 12,
                "k8s_setup_started_at": "2026-09-11T02:16:30Z",
                "k8s_setup_completed_at": "2026-09-11T02:17:30Z",
                "finalizer_seconds": 32,
            },
            "custom_status": {
                "steps": {
                    "running": {
                        "k8s": {
                            "blast_container_duration_ms": 22_000,
                            "results_export_container_duration_ms": 8_000,
                        }
                    }
                }
            },
        }
    )

    assert timing["phases"] == {
        "orchestration_seconds": 12,
        "k8s_setup_seconds": 60,
        "blast_seconds": 22,
        "export_seconds": 8,
        "finalizer_seconds": 32,
    }
