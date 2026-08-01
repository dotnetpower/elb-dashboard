"""Regression tests for Celery worker queue isolation and stale-tick expiry.

Responsibility: Verify interactive, reconcile, and artifact queues cannot share
    the default worker pool, Service Bus has a dedicated worker, and obsolete
    periodic ticks expire.
Edit boundaries: Worker topology and Celery schedule contracts only; task domain
    behaviour belongs in its focused task test module.
Key entry points: the `test_*` functions.
Risky contracts: The default topology must preserve a dedicated interactive pool
    without increasing the worker sidecar's total prefork child count.
Validation: `uv run pytest -q api/tests/test_celery_queue_isolation.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api import celery_signals, run_celery_workers
from api.celery_app import celery_app


def test_default_worker_topology_physically_isolates_reconcile_queue() -> None:
    specs = {
        name: (queues.split(","), int(concurrency))
        for name, queues, concurrency in run_celery_workers._worker_specs()
    }

    assert set(specs) == {
        "worker-main",
        "worker-reconcile",
        "worker-servicebus",
        "worker-artifacts",
    }
    assert specs["worker-reconcile"][0] == ["reconcile"]
    assert specs["worker-servicebus"][0] == ["servicebus"]
    assert "reconcile" not in specs["worker-main"][0]
    assert "reconcile" not in specs["worker-artifacts"][0]
    assert "servicebus" not in specs["worker-reconcile"][0]
    assert set(specs["worker-main"][0]).isdisjoint(specs["worker-artifacts"][0])
    # Five prefork children preserve the worker-sidecar memory envelope.
    assert sum(concurrency for _, concurrency in specs.values()) == 5


def test_worker_topology_rejects_queue_overlap_from_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_celery_workers,
        "MAIN_QUEUES",
        "default,azure,reconcile",
    )

    with pytest.raises(ValueError, match=r"both consume.*reconcile"):
        run_celery_workers._worker_specs()


def test_only_servicebus_parent_owns_sidecar_background_consumers() -> None:
    assert celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-servicebus@replica")
    )
    assert not celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-reconcile@replica")
    )
    assert not celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-main@replica")
    )
    assert not celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-artifacts@replica")
    )


def test_servicebus_parent_initialises_telemetry_and_consumers_post_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.app import telemetry
    from api.services import service_bus_external_consumer
    from api.services.blast import resident_consumer

    calls: list[str] = []
    monkeypatch.setattr(
        telemetry,
        "init_telemetry",
        lambda *, role, app=None: calls.append(f"telemetry:{role}") or True,
    )
    monkeypatch.setattr(
        resident_consumer,
        "start_resident_consumer",
        lambda: calls.append("resident") or True,
    )
    monkeypatch.setattr(
        service_bus_external_consumer,
        "start_external_consumer",
        lambda: calls.append("external") or True,
    )

    celery_signals._on_worker_ready(
        sender=SimpleNamespace(hostname="worker-servicebus@replica")
    )

    assert calls == ["telemetry:worker", "resident", "external"]


def test_servicebus_periodic_ticks_expire_before_stale_backlog_replays() -> None:
    schedule = celery_app.conf.beat_schedule

    for entry_name in (
        "servicebus-drain-and-resubmit",
        "servicebus-publish-transitions",
    ):
        options = schedule[entry_name]["options"]
        assert options["queue"] == "servicebus"
        assert 0 < float(options["expires"]) <= 30


def test_servicebus_health_tick_is_bounded_and_isolated() -> None:
    entry = celery_app.conf.beat_schedule["servicebus-health-telemetry"]
    options = entry["options"]

    assert entry["task"] == "api.tasks.servicebus.emit_service_bus_health"
    assert options["queue"] == "servicebus"
    assert 0 < float(options["expires"]) < float(entry["schedule"])


def test_warmup_tick_expires_before_next_schedule() -> None:
    schedule = celery_app.conf.beat_schedule

    entry = schedule["auto-warmup-reconcile"]
    options = entry["options"]
    assert options["queue"] == "reconcile"
    assert 0 < float(options["expires"]) < float(entry["schedule"])


def test_runtime_metrics_backfill_is_slow_cadence_and_non_poison() -> None:
    entry = celery_app.conf.beat_schedule["blast-backfill-completed-runtime-metrics"]
    task = celery_app.tasks["api.tasks.blast.backfill_completed_runtime_metrics"]

    assert float(entry["schedule"]) >= 3600
    assert entry["options"]["queue"] == "reconcile"
    assert 0 < float(entry["options"]["expires"]) < float(entry["schedule"])
    assert task.acks_late is False
    assert task.reject_on_worker_lost is False
    assert task.soft_time_limit == 240
    assert task.time_limit == 300


def test_autostop_tick_isolated_on_interactive_azure_queue() -> None:
    entry = celery_app.conf.beat_schedule["aks-idle-autostop-evaluate"]
    options = entry["options"]

    assert options["queue"] == "azure"
    assert 0 < float(options["expires"]) < float(entry["schedule"])
