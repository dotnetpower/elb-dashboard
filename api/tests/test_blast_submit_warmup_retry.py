"""Submit-task behaviour when node-local DB warmup is not ready yet.

Responsibility: Exercise the ``WarmupNotReadyError`` branch in
``api.tasks.blast.submit_task.submit`` so a *retryable* warmup state re-enqueues
the submit as ``queued`` on the ``waiting_for_warmup`` phase (a Pending/Queued
state in the UI) instead of failing the job, while a *non-retryable* warmup
error still fails fast as ``warmup_not_ready``. Also pin the contract that the
baseline run profile (``enable_warmup`` false) skips the warmup gate entirely so
it can run immediately, and that the re-enqueue does not consume the task's
``max_retries`` budget while still being bounded by a generous warmup-wait
deadline carried in the re-enqueue message.
Edit boundaries: Stub only the pipeline up to the warmup poll (DB availability,
sharding suppression, az-login / oracle parallel futures) so the test isolates
the warmup branch. Do NOT reach the capacity gate, submit lock, or terminal
stream — the warmup error short-circuits before them.
Key entry points: ``test_submit_retryable_warmup_requeues_as_queued``,
``test_warmup_requeue_stamps_and_forwards_deadline``,
``test_warmup_wait_deadline_exceeded_fails``,
``test_submit_non_retryable_warmup_fails``,
``test_submit_requeue_failure_falls_back_to_retry``,
``test_requeued_warmup_row_stays_active_in_reconciler``,
``test_baseline_profile_runs_without_node_warmup_poll``.
Risky contracts: Helpers are re-exported through ``api.tasks.blast`` (``_blast``);
monkeypatching them on that module is what the submit task actually resolves.
The ``submit`` re-enqueue is asserted via a patched ``submit.apply_async``. The
requeued waiting row MUST keep ``status="running"`` so the reconciler's
``_celery_success_row_status`` keeps it active instead of completing it.
Validation: ``uv run pytest -q api/tests/test_blast_submit_warmup_retry.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.blast.task_config import (
    WarmupNotReadyError,
    ensure_node_warmup_ready_for_submit,
    submit_requires_node_warmup,
)
from api.tasks import blast as _blast
from api.tasks.blast.reconcile_task import _celery_success_row_status

_SUBMIT_KWARGS = dict(
    job_id="job-warmup-1",
    subscription_id="sub-1",
    resource_group="rg-elb",
    cluster_name="aks-elb",
    storage_account="elbstg01",
    program="blastn",
    database="core_nt",
    query_file="queries/q.fa",
    options={"sharding_mode": "precise", "enable_warmup": True},
)


def _install_warmup_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Stub the submit pipeline up to (and including) the warmup poll."""

    updates: list[tuple[str, str, dict[str, Any]]] = []

    def _update_state(job_id: str, phase: str, status: str = "running", **details: Any) -> None:
        updates.append((job_id, phase, {"status": status, **details}))

    monkeypatch.setattr(_blast, "_update_state", _update_state)
    monkeypatch.setattr(_blast, "_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(
        _blast,
        "_suppress_sharding_for_unsharded_database",
        lambda **kwargs: kwargs.get("options"),
    )
    monkeypatch.setattr(
        _blast, "_expand_strict_tie_order_candidate_pool", lambda options: options
    )
    monkeypatch.setattr(_blast, "_validate_blast_database_available", lambda **_k: None)
    monkeypatch.setattr(_blast, "_validate_blast_database_ready", lambda **_k: None)
    monkeypatch.setattr(_blast, "_requires_split_parent_submission", lambda *_a, **_k: False)
    monkeypatch.setattr(_blast, "_ensure_terminal_azure_cli_login", lambda *_a, **_k: None)
    monkeypatch.setattr(_blast, "_snippet", lambda value, *_a, **_k: str(value)[:200])
    # The az-login + oracle uploads fan out in parallel with the warmup poll but
    # are only awaited after a successful warmup, so a no-op keeps them inert.
    monkeypatch.setattr(
        "api.tasks.blast.submit_task.upload_tie_order_oracle_if_present",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "api.tasks.blast.submit_task.upload_db_order_oracle_pointer_if_available",
        lambda **_k: None,
    )
    return updates


def _patch_apply_async(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture ``submit.apply_async`` re-enqueue calls without a broker."""

    enqueued: list[dict[str, Any]] = []

    def _apply_async(*_args: Any, **kwargs: Any) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(_blast.submit, "apply_async", _apply_async)
    return enqueued


def test_submit_retryable_warmup_requeues_as_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    updates = _install_warmup_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)

    def _raise_retryable(**_kwargs: Any) -> None:
        raise WarmupNotReadyError(
            "node warmup for core_nt has no DB generation marker",
            retryable=True,
        )

    monkeypatch.setattr(_blast, "_ensure_node_warmup_ready_for_submit", _raise_retryable)
    monkeypatch.setattr(
        _blast,
        "_retry_or_fail",
        lambda *_a, **_k: pytest.fail("retryable warmup must re-enqueue, not task.retry"),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    # While warmup settles the job is re-enqueued (no max_retries budget
    # consumed) and the waiting row keeps status="running" on the
    # waiting_for_warmup phase — exactly like the capacity gate — so the
    # reconciler keeps it active instead of completing it prematurely.
    assert result == {
        "job_id": "job-warmup-1",
        "status": "running",
        "phase": "waiting_for_warmup",
        "requeued": True,
    }
    assert len(enqueued) == 1
    enq = enqueued[0]
    assert enq["countdown"] == 30
    assert enq["queue"] == "blast"
    assert enq["kwargs"]["job_id"] == "job-warmup-1"
    # The re-enqueue forwards the ORIGINAL options (warmup recomputed next run).
    assert enq["kwargs"]["options"] == {"sharding_mode": "precise", "enable_warmup": True}

    # A running waiting row is written, and the job is NOT failed.
    waiting_rows = [u for u in updates if u[1] == "waiting_for_warmup"]
    assert waiting_rows
    assert waiting_rows[-1][2]["status"] == "running"
    assert waiting_rows[-1][2]["error_code"] == "node_warmup_not_ready"
    assert not [u for u in updates if u[2].get("status") == "failed"]


def test_warmup_requeue_stamps_and_forwards_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first re-enqueue stamps a deadline; later re-enqueues forward it."""

    _install_warmup_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)
    monkeypatch.setenv("BLAST_WARMUP_MAX_WAIT_SECONDS", "1800")
    monkeypatch.setattr("api.tasks.blast.submit_task.time.time", lambda: 1000.0)

    def _raise_retryable(**_kwargs: Any) -> None:
        raise WarmupNotReadyError("warmup loading", retryable=True)

    monkeypatch.setattr(_blast, "_ensure_node_warmup_ready_for_submit", _raise_retryable)
    monkeypatch.setattr(_blast, "_retry_or_fail", lambda *_a, **_k: pytest.fail("must re-enqueue"))

    # First wait: no deadline carried -> stamp now (1000) + 1800 = 2800.
    _blast.submit.run(**_SUBMIT_KWARGS)
    assert enqueued[-1]["kwargs"]["warmup_wait_deadline_ts"] == 2800.0

    # Subsequent wait still well before the deadline forwards it unchanged.
    _blast.submit.run(**_SUBMIT_KWARGS, warmup_wait_deadline_ts=2800.0)
    assert enqueued[-1]["kwargs"]["warmup_wait_deadline_ts"] == 2800.0


def test_warmup_wait_deadline_exceeded_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the carried deadline passes, the warmup wait fails instead of looping."""

    updates = _install_warmup_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)
    monkeypatch.setattr("api.tasks.blast.submit_task.time.time", lambda: 5000.0)

    def _raise_retryable(**_kwargs: Any) -> None:
        raise WarmupNotReadyError("warmup still loading", retryable=True)

    monkeypatch.setattr(_blast, "_ensure_node_warmup_ready_for_submit", _raise_retryable)
    monkeypatch.setattr(
        _blast,
        "_retry_or_fail",
        lambda *_a, **_k: pytest.fail("deadline path fails fast, no retry budget"),
    )

    # Deadline (2800) already in the past relative to now (5000).
    result = _blast.submit.run(**_SUBMIT_KWARGS, warmup_wait_deadline_ts=2800.0)

    assert result["status"] == "failed"
    assert result["phase"] == "warmup_not_ready"
    assert result["error_code"] == "node_warmup_wait_deadline_exceeded"
    # No further re-enqueue once the deadline is exceeded.
    assert enqueued == []
    failed_rows = [u for u in updates if u[2].get("status") == "failed"]
    assert failed_rows
    assert failed_rows[-1][2]["error_code"] == "node_warmup_wait_deadline_exceeded"


def test_submit_non_retryable_warmup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    updates = _install_warmup_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)

    def _raise_hard(**_kwargs: Any) -> None:
        raise WarmupNotReadyError(
            "node warmup readiness cannot be checked without a database name",
            retryable=False,
        )

    monkeypatch.setattr(_blast, "_ensure_node_warmup_ready_for_submit", _raise_hard)
    monkeypatch.setattr(
        _blast,
        "_retry_or_fail",
        lambda *_a, **_k: pytest.fail("_retry_or_fail must not run on a non-retryable warmup"),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "failed"
    assert result["phase"] == "warmup_not_ready"
    assert result["error_code"] == "node_warmup_not_ready"
    # A non-retryable warmup never re-enqueues.
    assert enqueued == []
    failed_rows = [u for u in updates if u[1] == "warmup_not_ready" and u[2]["status"] == "failed"]
    assert failed_rows
    assert failed_rows[-1][2]["error_code"] == "node_warmup_not_ready"


class _Row:
    """Minimal stand-in for the state row the reconciler inspects."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.status = "running"
        self.phase = "waiting_for_warmup"
        self.task_id = "task-1"
        self.storage_account = "elbstg01"


def test_requeued_warmup_row_stays_active_in_reconciler() -> None:
    """The requeued SUCCESS result must NOT be reconciled to ``completed``.

    The original submit task returns SUCCESS the moment it re-enqueues, so the
    reconciler sees that terminal Celery state while the job is still waiting.
    ``_celery_success_row_status`` only keeps ``status="running"`` active; a
    ``"queued"`` result would fall through to ``completed`` and fire the artifact
    finalizer on a job that has not run yet. This pins the contract that the
    warmup re-enqueue uses ``"running"`` (matching the capacity gate).
    """

    row = _Row("job-warmup-1")

    # What the warmup re-enqueue actually returns.
    status, phase = _celery_success_row_status(
        row, {"status": "running", "phase": "waiting_for_warmup", "requeued": True}
    )
    assert status == "running"
    assert phase == "waiting_for_warmup"

    # Guard rail: prove the old ``"queued"`` shape WOULD have been mis-completed,
    # documenting why ``"running"`` is required.
    bad_status, _ = _celery_success_row_status(
        row, {"status": "queued", "phase": "waiting_for_warmup", "requeued": True}
    )
    assert bad_status == "completed"


def test_submit_requeue_failure_falls_back_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the broker is gone, re-enqueue fails over to the bounded retry path."""

    _install_warmup_stubs(monkeypatch)

    def _raise_retryable(**_kwargs: Any) -> None:
        raise WarmupNotReadyError("warmup loading", retryable=True)

    monkeypatch.setattr(_blast, "_ensure_node_warmup_ready_for_submit", _raise_retryable)

    def _apply_async_boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(_blast.submit, "apply_async", _apply_async_boom)

    retry_calls: list[dict[str, Any]] = []

    def _fake_retry_or_fail(_task: Any, **kwargs: Any) -> dict[str, Any]:
        retry_calls.append(kwargs)
        return {"job_id": kwargs["job_id"], "status": "running", "phase": kwargs["phase"]}

    monkeypatch.setattr(_blast, "_retry_or_fail", _fake_retry_or_fail)

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert len(retry_calls) == 1
    assert retry_calls[0]["phase"] == "waiting_for_warmup"
    assert retry_calls[0]["error_code"] == "blast_submit_requeue_failed"
    assert result["phase"] == "waiting_for_warmup"


# ---------------------------------------------------------------------------
# Baseline run profile: enable_warmup is false -> warmup gate is skipped, so the
# search can run immediately (no waiting_for_warmup, no node warmup poll).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        # Baseline profile as set by the New Search ComputeSection: warmup off,
        # sharding off, no auto-partition.
        {"enable_warmup": False, "sharding_mode": "off", "db_auto_partition": False},
        # enable_warmup=false must win even if a sharded mode is left selected.
        {"enable_warmup": False, "sharding_mode": "precise"},
    ],
)
def test_baseline_profile_skips_warmup_requirement(options: dict[str, Any]) -> None:
    assert submit_requires_node_warmup(options) is False


def test_baseline_profile_runs_without_node_warmup_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """A baseline submit never touches the K8s warmup status (runs immediately)."""

    def _fail_warmup_status(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("baseline run profile must not poll node warmup")

    monkeypatch.setattr("api.services.monitoring.k8s_warmup_status", _fail_warmup_status)

    assert (
        ensure_node_warmup_ready_for_submit(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="aks-elb",
            database="core_nt",
            storage_account="elbstg01",
            options={"enable_warmup": False, "sharding_mode": "off"},
            metadata_resolver=lambda *_a, **_k: None,
        )
        is None
    )
