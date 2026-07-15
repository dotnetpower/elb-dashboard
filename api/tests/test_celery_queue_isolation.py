"""Regression tests for Celery worker queue isolation and stale-tick expiry.

Responsibility: Verify interactive, reconcile, and artifact queues cannot share
    the default worker pool and that obsolete Service Bus periodic ticks expire.
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

    assert set(specs) == {"worker-main", "worker-reconcile", "worker-artifacts"}
    assert specs["worker-reconcile"][0] == ["reconcile"]
    assert "reconcile" not in specs["worker-main"][0]
    assert "reconcile" not in specs["worker-artifacts"][0]
    assert set(specs["worker-main"][0]).isdisjoint(specs["worker-artifacts"][0])
    # Five children plus the added reconcile parent keeps the total Python
    # process count equal to the prior two-parent/six-child topology.
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


def test_only_reconcile_parent_owns_sidecar_background_consumers() -> None:
    assert celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-reconcile@replica")
    )
    assert not celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-main@replica")
    )
    assert not celery_signals._is_background_consumer_worker(
        SimpleNamespace(hostname="worker-artifacts@replica")
    )


def test_servicebus_periodic_ticks_expire_before_stale_backlog_replays() -> None:
    schedule = celery_app.conf.beat_schedule

    for entry_name in (
        "servicebus-drain-and-resubmit",
        "servicebus-publish-transitions",
    ):
        options = schedule[entry_name]["options"]
        assert options["queue"] == "reconcile"
        assert 0 < float(options["expires"]) <= 30


def test_cost_and_warmup_ticks_expire_before_next_schedule() -> None:
    schedule = celery_app.conf.beat_schedule

    for entry_name in ("auto-warmup-reconcile", "aks-idle-autostop-evaluate"):
        entry = schedule[entry_name]
        options = entry["options"]
        assert options["queue"] == "reconcile"
        assert 0 < float(options["expires"]) < float(entry["schedule"])
