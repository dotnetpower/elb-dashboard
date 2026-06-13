"""Tests for `api.services.db.stale_dbops` — the dbops/warmup stale-row reconciler.

Responsibility: Cover every branch of the pure ``classify_dbops_row`` decision
    function, the per-row IO glue ``reconcile_dbops_decision`` (Celery probe +
    terminal write), and the orchestrator ``reconcile_dbops`` (scan + tally +
    kill-switch). No real Table / Celery — a fake repo and a fake AsyncResult
    cover every path.
Edit boundaries: Pure unit tests. When a new dbops ``type`` or terminal reason
    is added to the service, extend the matrix here.
Key entry points: see per-test docstrings.
Risky contracts: A synchronous op must terminalise to ``completed`` regardless
    of age; an async op must NEVER be aged out below its per-type threshold
    (a live ``nt`` download legitimately runs hours).
Validation: `uv run pytest -q api/tests/test_stale_dbops_reconcile.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from api.services.db import stale_dbops
from api.services.db.stale_dbops import (
    RECONCILE_TYPES,
    classify_dbops_row,
    reconcile_dbops,
    reconcile_dbops_decision,
)

_NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# classify_dbops_row — pure decision matrix
# --------------------------------------------------------------------------- #


def test_synchronous_op_terminalizes_completed_regardless_of_age() -> None:
    """A `prepare_db_cancel` / `prepare_db_delete` row is born terminal now, but
    a pre-existing queued one must be driven to `completed` with no Celery
    probe and no age gate."""
    for op in ("prepare_db_cancel", "prepare_db_delete"):
        d = classify_dbops_row(
            row_type=op,
            status="queued",
            updated_at=_iso(_NOW),  # brand new
            created_at=_iso(_NOW),
            has_task_id=False,
            celery_state=None,
            now=_NOW,
        )
        assert d.action == "terminalize"
        assert d.status == "completed"
        assert d.reason == "synchronous-op-completed"


def test_celery_success_completes_async_row() -> None:
    d = classify_dbops_row(
        row_type="warmup",
        status="running",
        updated_at=_iso(_NOW),
        created_at=_iso(_NOW),
        has_task_id=True,
        celery_state="SUCCESS",
        now=_NOW,
    )
    assert d.action == "terminalize"
    assert d.status == "completed"
    assert d.reason == "celery-success"


@pytest.mark.parametrize("state", ["FAILURE", "REVOKED"])
def test_celery_terminal_failed_fails_async_row(state: str) -> None:
    d = classify_dbops_row(
        row_type="prepare_db_aks",
        status="queued",
        updated_at=_iso(_NOW),
        created_at=_iso(_NOW),
        has_task_id=True,
        celery_state=state,
        now=_NOW,
    )
    assert d.action == "terminalize"
    assert d.status == "failed"
    assert d.error_code == "task_failed"
    assert d.reason == "celery-terminal-failed"


def test_aged_out_async_row_fails_worker_lost() -> None:
    """A prepare_db row quiet past its threshold with no live Celery record is a
    crashed-worker zombie → failed/worker_lost."""
    old = _NOW - timedelta(seconds=stale_dbops._PREPARE_DB_STALE_SECONDS + 60)
    d = classify_dbops_row(
        row_type="prepare_db",
        status="queued",
        updated_at=_iso(old),
        created_at=_iso(old),
        has_task_id=False,
        celery_state=None,
        now=_NOW,
    )
    assert d.action == "terminalize"
    assert d.status == "failed"
    assert d.error_code == "worker_lost"
    assert d.reason == "aged-out-worker-lost"


def test_live_download_within_threshold_is_skipped() -> None:
    """A real download legitimately runs for hours — a row a few minutes old
    with a PENDING task must NOT be aged out."""
    recent = _NOW - timedelta(seconds=600)
    d = classify_dbops_row(
        row_type="prepare_db_aks",
        status="running",
        updated_at=_iso(recent),
        created_at=_iso(recent),
        has_task_id=True,
        celery_state="PENDING",
        now=_NOW,
    )
    assert d.action == "skip"
    assert d.reason in {"task-live", "within-threshold"}


def test_warmup_threshold_is_shorter_than_prepare_db() -> None:
    """A warmup row quiet past the (shorter) warmup threshold but well within
    the prepare_db threshold is aged out — proving per-type thresholds apply."""
    quiet = _NOW - timedelta(seconds=stale_dbops._WARMUP_STALE_SECONDS + 60)
    assert stale_dbops._WARMUP_STALE_SECONDS < stale_dbops._PREPARE_DB_STALE_SECONDS
    d = classify_dbops_row(
        row_type="warmup",
        status="running",
        updated_at=_iso(quiet),
        created_at=_iso(quiet),
        has_task_id=True,
        celery_state="PENDING",
        now=_NOW,
    )
    assert d.action == "terminalize"
    assert d.error_code == "worker_lost"


def test_already_terminal_row_is_skipped() -> None:
    d = classify_dbops_row(
        row_type="warmup",
        status="completed",
        updated_at=_iso(_NOW),
        created_at=_iso(_NOW),
        has_task_id=True,
        celery_state="SUCCESS",
        now=_NOW,
    )
    assert d.action == "skip"
    assert d.reason == "already-terminal"


def test_unmanaged_type_is_skipped() -> None:
    d = classify_dbops_row(
        row_type="blast",
        status="running",
        updated_at=_iso(_NOW),
        created_at=_iso(_NOW),
        has_task_id=True,
        celery_state="PENDING",
        now=_NOW,
    )
    assert d.action == "skip"
    assert d.reason == "type-not-managed"


def test_unparseable_timestamp_fails_safe() -> None:
    """No parseable timestamp on a quiet async row → keep it (a later tick or a
    terminal Celery state resolves it) rather than guess a terminal status."""
    d = classify_dbops_row(
        row_type="warmup",
        status="running",
        updated_at="not-a-date",
        created_at="",
        has_task_id=True,
        celery_state="PENDING",
        now=_NOW,
    )
    assert d.action == "skip"
    assert d.reason == "no-timestamp"


# --------------------------------------------------------------------------- #
# reconcile_dbops_decision — IO glue
# --------------------------------------------------------------------------- #


@dataclass
class _Row:
    job_id: str
    type: str
    status: str
    updated_at: str
    created_at: str
    task_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class _FakeRepo:
    def __init__(self, rows_by_type: dict[str, list[_Row]] | None = None) -> None:
        self._rows = rows_by_type or {}
        self.updates: list[tuple[str, str, str, str | None]] = []
        self.history: list[tuple[str, str]] = []
        self.raise_missing: set[str] = set()

    def list_active(self, *, job_type: str = "blast", limit: int = 500) -> list[_Row]:
        return list(self._rows.get(job_type, []))

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if job_id in self.raise_missing:
            raise KeyError(job_id)
        self.updates.append((job_id, status or "", phase or "", error_code))

    def append_history(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        self.history.append((job_id, event))


class _FakeAsyncResult:
    _states: ClassVar[dict[str, str]] = {}

    def __init__(self, task_id: str, app: Any = None) -> None:
        self.status = self._states.get(task_id, "PENDING")


def _patch_async(monkeypatch: pytest.MonkeyPatch, states: dict[str, str]) -> None:
    _FakeAsyncResult._states = states
    monkeypatch.setattr("celery.result.AsyncResult", _FakeAsyncResult)


def test_decision_reads_task_id_from_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prepare_db_aks` stores its task id in the payload, not the task_id
    column — the glue must look there and honour a terminal Celery state."""
    _patch_async(monkeypatch, {"task-123": "SUCCESS"})
    repo = _FakeRepo()
    row = _Row(
        job_id="dbops:prepare_db_aks:acct:nt:abc",
        type="prepare_db_aks",
        status="queued",
        updated_at=_iso(_NOW),
        created_at=_iso(_NOW),
        payload={"task_id": "task-123"},
    )
    reason = reconcile_dbops_decision(repo, row, celery_app=None, now=_NOW)
    assert reason == "celery-success"
    assert repo.updates == [(row.job_id, "completed", "completed", None)]
    assert repo.history == [(row.job_id, "completed")]


def test_decision_skips_when_row_vanishes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async(monkeypatch, {})
    repo = _FakeRepo()
    repo.raise_missing.add("gone")
    row = _Row(
        job_id="gone",
        type="prepare_db_cancel",
        status="queued",
        updated_at=_iso(_NOW),
        created_at=_iso(_NOW),
    )
    reason = reconcile_dbops_decision(repo, row, celery_app=None, now=_NOW)
    assert reason == "row-gone"


# --------------------------------------------------------------------------- #
# reconcile_dbops — orchestrator
# --------------------------------------------------------------------------- #


def _patch_orchestrator(
    monkeypatch: pytest.MonkeyPatch, repo: _FakeRepo, states: dict[str, str]
) -> None:
    _patch_async(monkeypatch, states)
    monkeypatch.setattr(
        "api.services.state_repo.JobStateRepository", lambda *a, **k: repo
    )

    class _App:  # celery_app stand-in
        pass

    import api.celery_app as celery_module

    monkeypatch.setattr(celery_module, "celery_app", _App(), raising=False)


def test_orchestrator_tallies_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The orchestrator stamps its own `now = datetime.now(UTC)`, so anchor the
    # row timestamps to the real clock rather than the module-level `_NOW`.
    now = datetime.now(UTC)
    fresh = _iso(now)
    old = _iso(now - timedelta(seconds=stale_dbops._PREPARE_DB_STALE_SECONDS + 60))
    rows = {
        "warmup": [
            _Row("w-ok", "warmup", "running", fresh, fresh, task_id="t-ok"),
        ],
        "prepare_db": [
            _Row("p-lost", "prepare_db", "queued", old, old),
        ],
        "prepare_db_cancel": [
            _Row("c-1", "prepare_db_cancel", "queued", fresh, fresh),
        ],
        "prepare_db_aks": [
            _Row(
                "a-live",
                "prepare_db_aks",
                "running",
                fresh,
                fresh,
                task_id="t-live",
            ),
        ],
    }
    repo = _FakeRepo(rows)
    _patch_orchestrator(monkeypatch, repo, {"t-ok": "SUCCESS", "t-live": "STARTED"})

    summary = reconcile_dbops(enabled=True)
    assert summary["scanned"] == 4
    assert summary["completed"] == 2  # warmup SUCCESS + sync cancel
    assert summary["failed"] == 1  # aged-out prepare_db
    assert summary["skipped"] == 1  # live prepare_db_aks
    assert summary["errors"] == 0
    written = {u[0]: (u[1], u[3]) for u in repo.updates}
    assert written["w-ok"] == ("completed", None)
    assert written["c-1"] == ("completed", None)
    assert written["p-lost"] == ("failed", "worker_lost")
    assert "a-live" not in written


def test_orchestrator_kill_switch_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo({"warmup": [_Row("w", "warmup", "running", _iso(_NOW), _iso(_NOW))]})
    _patch_orchestrator(monkeypatch, repo, {})
    summary = reconcile_dbops(enabled=False)
    assert summary["disabled"] is True
    assert summary["scanned"] == 0
    assert repo.updates == []


def test_reconcile_types_cover_issue_evidence() -> None:
    """The reconciler must own every row type from the issue-34 evidence
    table plus warmup."""
    for required in (
        "warmup",
        "prepare_db",
        "prepare_db_aks",
        "prepare_db_cancel",
        "prepare_db_delete",
    ):
        assert required in RECONCILE_TYPES
