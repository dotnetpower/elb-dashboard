"""Submit-task behaviour when the BLAST database is still copying / updating.

Responsibility: Exercise the ``BlastDatabaseAvailabilityError`` branch in
``api.tasks.blast.submit_task.submit`` so a *transient* DB state
(``database_not_ready`` = prepare-db copy in progress, ``database_updating`` =
version update mid-flight) re-enqueues the submit on the ``waiting_for_database``
phase (status="running", a Pending state in the UI) instead of failing the job,
while a *permanent* error (``database_not_found`` etc.) still fails fast as
``database_unavailable``. Also pin that the re-enqueue does not consume the
task's ``max_retries`` budget and is bounded by a database-wait deadline carried
in the re-enqueue message. This closes the gap where a submit accepted before
the DB finished warming — notably the OpenAPI path, which has no submit-time
readiness gate — would BLAST against incomplete volumes or fail outright.
Edit boundaries: Stub only the DB readiness check (``_validate_blast_database_ready``)
so the test isolates that branch; the transient path short-circuits before the
warmup poll, capacity gate, submit lock, and terminal stream.
Key entry points: ``test_transient_database_requeues_as_running``,
``test_database_requeue_stamps_and_forwards_deadline``,
``test_database_wait_deadline_exceeded_fails``,
``test_permanent_database_error_fails_fast``,
``test_database_requeue_failure_falls_back_to_retry``,
``test_requeued_database_row_stays_active_in_reconciler``.
Risky contracts: Helpers are re-exported through ``api.tasks.blast`` (``_blast``);
monkeypatching them on that module is what the submit task resolves. The
requeued waiting row MUST keep ``status="running"`` so the reconciler's
``_celery_success_row_status`` keeps it active instead of completing it.
Validation: ``uv run pytest -q api/tests/test_blast_submit_database_retry.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks import blast as _blast
from api.tasks.blast.config_shims import BlastDatabaseAvailabilityError
from api.tasks.blast.reconcile_task import _celery_success_row_status

_SUBMIT_KWARGS = dict(
    job_id="job-db-1",
    subscription_id="sub-1",
    resource_group="rg-elb",
    cluster_name="aks-elb",
    storage_account="elbstg01",
    program="blastn",
    database="core_nt",
    query_file="queries/q.fa",
    options={"sharding_mode": "precise", "enable_warmup": True},
)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Stub the submit pipeline up to (and including) the DB readiness check."""

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
    monkeypatch.setattr(_blast, "_snippet", lambda value, *_a, **_k: str(value)[:200])
    return updates


def _patch_apply_async(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture ``submit.apply_async`` re-enqueue calls without a broker."""

    enqueued: list[dict[str, Any]] = []

    def _apply_async(*_args: Any, **kwargs: Any) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(_blast.submit, "apply_async", _apply_async)
    return enqueued


@pytest.mark.parametrize("code", ["database_not_ready", "database_updating"])
def test_transient_database_requeues_as_running(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    updates = _install_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)

    def _raise(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError("db still copying", code=code)

    monkeypatch.setattr(_blast, "_validate_blast_database_ready", _raise)
    monkeypatch.setattr(
        _blast,
        "_retry_or_fail",
        lambda *_a, **_k: pytest.fail("transient DB must re-enqueue, not task.retry"),
    )

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result == {
        "job_id": "job-db-1",
        "status": "running",
        "phase": "waiting_for_database",
        "requeued": True,
        "error_code": code,
    }
    assert len(enqueued) == 1
    enq = enqueued[0]
    assert enq["countdown"] == 30
    assert enq["queue"] == "blast"
    assert enq["kwargs"]["job_id"] == "job-db-1"
    assert enq["kwargs"]["options"] == {"sharding_mode": "precise", "enable_warmup": True}

    waiting_rows = [u for u in updates if u[1] == "waiting_for_database"]
    assert waiting_rows
    assert waiting_rows[-1][2]["status"] == "running"
    assert waiting_rows[-1][2]["error_code"] == code
    assert not [u for u in updates if u[2].get("status") == "failed"]


def test_database_requeue_stamps_and_forwards_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first re-enqueue stamps a deadline; later re-enqueues forward it."""

    _install_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)
    monkeypatch.setenv("BLAST_DATABASE_MAX_WAIT_SECONDS", "1800")
    monkeypatch.setattr("api.tasks.blast.submit_task.time.time", lambda: 1000.0)

    def _raise(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError("copying", code="database_not_ready")

    monkeypatch.setattr(_blast, "_validate_blast_database_ready", _raise)

    # First run stamps now + 1800.
    _blast.submit.run(**_SUBMIT_KWARGS)
    assert enqueued[0]["kwargs"]["database_wait_deadline_ts"] == pytest.approx(2800.0)

    # A later run that already carries the deadline forwards it unchanged.
    _blast.submit.run(**_SUBMIT_KWARGS, database_wait_deadline_ts=2800.0)
    assert enqueued[1]["kwargs"]["database_wait_deadline_ts"] == pytest.approx(2800.0)


def test_database_wait_deadline_exceeded_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    updates = _install_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)
    monkeypatch.setattr("api.tasks.blast.submit_task.time.time", lambda: 5000.0)

    def _raise(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError("still copying", code="database_not_ready")

    monkeypatch.setattr(_blast, "_validate_blast_database_ready", _raise)

    # Deadline already passed -> fail fast, no re-enqueue.
    result = _blast.submit.run(**_SUBMIT_KWARGS, database_wait_deadline_ts=4000.0)

    assert result["status"] == "failed"
    assert result["phase"] == "database_unavailable"
    assert result["error_code"] == "database_not_ready"
    assert not enqueued
    assert [u for u in updates if u[2].get("status") == "failed"]


def test_permanent_database_error_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    updates = _install_stubs(monkeypatch)
    enqueued = _patch_apply_async(monkeypatch)

    def _raise(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError("no such db", code="database_not_found")

    monkeypatch.setattr(_blast, "_validate_blast_database_ready", _raise)

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert result["status"] == "failed"
    assert result["phase"] == "database_unavailable"
    assert result["error_code"] == "database_not_found"
    assert not enqueued
    assert [u for u in updates if u[2].get("status") == "failed"]


def test_database_requeue_failure_falls_back_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the re-enqueue itself fails (broker gone), fall back to bounded retry."""

    _install_stubs(monkeypatch)

    def _raise(**_kwargs: Any) -> None:
        raise BlastDatabaseAvailabilityError("copying", code="database_not_ready")

    monkeypatch.setattr(_blast, "_validate_blast_database_ready", _raise)

    def _boom_apply_async(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(_blast.submit, "apply_async", _boom_apply_async)

    retry_calls: list[dict[str, Any]] = []

    def _retry_or_fail(_self: Any, **kwargs: Any) -> dict[str, Any]:
        retry_calls.append(kwargs)
        return {"job_id": kwargs["job_id"], "status": "retry", "phase": kwargs["phase"]}

    monkeypatch.setattr(_blast, "_retry_or_fail", _retry_or_fail)

    result = _blast.submit.run(**_SUBMIT_KWARGS)

    assert retry_calls
    assert retry_calls[0]["phase"] == "waiting_for_database"
    assert retry_calls[0]["error_code"] == "blast_submit_requeue_failed"
    assert result["phase"] == "waiting_for_database"


def test_requeued_database_row_stays_active_in_reconciler() -> None:
    """A waiting_for_database row keeps status=running so the reconciler keeps it active.

    The original submit task returns SUCCESS the moment it re-enqueues, so the
    reconciler sees that terminal Celery state while the job is still waiting.
    ``_celery_success_row_status`` only keeps ``status="running"`` active; a
    ``"queued"`` result would fall through to ``completed`` and fire the
    artifact finalizer on a job that has not run yet.
    """

    class _Row:
        def __init__(self) -> None:
            self.job_id = "job-db-1"
            self.status = "running"
            self.phase = "waiting_for_database"
            self.task_id = "task-1"
            self.storage_account = "elbstg01"

    status, phase = _celery_success_row_status(
        _Row(), {"status": "running", "phase": "waiting_for_database", "requeued": True}
    )
    assert status == "running"
    assert phase == "waiting_for_database"

    # Guard rail: the old "queued" shape WOULD be mis-completed — documents why
    # "running" is required for the waiting row.
    bad_status, _ = _celery_success_row_status(
        _Row(), {"status": "queued", "phase": "waiting_for_database", "requeued": True}
    )
    assert bad_status == "completed"
