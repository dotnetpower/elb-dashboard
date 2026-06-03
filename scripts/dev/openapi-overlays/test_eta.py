"""Unit tests for the elb-openapi ETA overlay (pure-python core, no Azure).

Responsibility: Verify feature parsing, online learning convergence, the
fallback chain, cold/warm separation, and the C-server queue simulation of
``eta.py`` without any Azure dependency (the store degrades to in-memory).
Edit boundaries: Test-only; keep in lockstep with ``eta.py`` public contract.
Key entry points: pytest test functions.
Risky contracts: Relies on ``eta._store`` being the in-memory fallback (no
table credentials in the test env) and on ``ELB_OPENAPI_ETA_ENABLED`` toggling
the public entry points.
Validation: ``uv run python -m pytest scripts/dev/openapi-overlays/test_eta.py``.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eta


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    """Enable ETA and reset the in-memory aggregate store before each test."""
    monkeypatch.setenv("ELB_OPENAPI_ETA_ENABLED", "true")
    importlib.reload(eta)
    eta._store._mem.clear()
    eta._store._mem_ts.clear()
    yield


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ── feature parsing ─────────────────────────────────────────────────────────


def test_parse_query_features_counts_seqs_and_bases():
    fasta = ">a\nACGTACGT\nACGT\n>b\nAAAA\n"
    seqs, bases = eta.parse_query_features(fasta)
    assert seqs == 2
    assert bases == 16


def test_parse_query_features_empty():
    assert eta.parse_query_features(None) == (0, 0)
    assert eta.parse_query_features("") == (0, 0)


def test_bucket_groups_nearby_sizes():
    assert eta.bucket_for(1, 500) == eta.bucket_for(1, 900)
    assert eta.bucket_for(1, 500) != eta.bucket_for(1, 50_000)


# ── learning / prediction ───────────────────────────────────────────────────


def test_predict_falls_back_to_db_default_when_cold_on_samples():
    est, conf, basis = eta.predict("core_nt", "s1.b1", cold=False)
    assert conf == "low"
    assert est == eta._DEFAULT_RUN_SECONDS["core_nt"]
    assert basis["samples"] == 0


def test_learning_converges_toward_observed_mean():
    pk = eta._partition_key("core_nt", "s1.b1", cold=False)
    for _ in range(12):
        eta._store.update(pk, 100.0)
    est, conf, basis = eta.predict("core_nt", "s1.b1", cold=False)
    assert conf == "high"
    assert basis["samples"] == 12
    # p65 bias on near-zero variance stays close to the mean.
    assert 99.0 <= est <= 103.0


def test_cold_and_warm_are_learned_separately():
    warm_pk = eta._partition_key("core_nt", "s1.b1", cold=False)
    cold_pk = eta._partition_key("core_nt", "s1.b1", cold=True)
    for _ in range(5):
        eta._store.update(warm_pk, 100.0)
        eta._store.update(cold_pk, 300.0)
    warm_est, _, _ = eta.predict("core_nt", "s1.b1", cold=False)
    cold_est, _, _ = eta.predict("core_nt", "s1.b1", cold=True)
    assert warm_est < 150.0
    assert cold_est > 250.0


def test_cold_falls_back_to_warm_when_no_cold_samples():
    warm_pk = eta._partition_key("core_nt", "s1.b1", cold=False)
    for _ in range(5):
        eta._store.update(warm_pk, 100.0)
    cold_est, _conf, basis = eta.predict("core_nt", "s1.b1", cold=True)
    assert basis["cold"] is False  # fell back to the warm row
    assert cold_est < 150.0


# ── queue simulation ────────────────────────────────────────────────────────


def _job(jid, status, *, created, started=None, completed=None, priority=50, seqs=1, bases=500):
    job = {
        "job_id": jid,
        "status": status,
        "created_at": created,
        "priority": priority,
        "db_name": "core_nt",
        "query_seqs": seqs,
        "query_bases": bases,
    }
    if started:
        job["started_at"] = started
    if completed:
        job["completed_at"] = completed
    return job


def test_compute_eta_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("ELB_OPENAPI_ETA_ENABLED", "false")
    importlib.reload(eta)
    assert eta.compute_eta({"status": "queued"}, [], 2) is None


def test_running_eta_is_remaining_time():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    started = now - timedelta(seconds=40)
    job = _job("j1", "running", created=_iso(now - timedelta(seconds=60)), started=_iso(started))
    out = eta.compute_eta(job, [job], 2, now=now)
    assert out is not None
    # default core_nt run ~110s, elapsed 40s => ~70s remaining.
    assert 60.0 <= out["remaining_seconds"] <= 80.0
    assert out["estimated_finish_at"].endswith("Z")


def test_queue_eta_staggers_by_server_count():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    base = now - timedelta(seconds=5)
    # 2 servers, all jobs freshly queued (no active). Per-job default ~110s.
    jobs = [
        _job(f"q{i}", "queued", created=_iso(base + timedelta(seconds=i)))
        for i in range(4)
    ]
    etas = {
        j["job_id"]: eta.compute_eta(j, jobs, 2, now=now) for j in jobs
    }
    # With C=2: jobs 0,1 start at 0; jobs 2,3 start after the first wave (~110s).
    assert etas["q0"]["estimated_start_seconds"] == pytest.approx(0.0, abs=1.0)
    assert etas["q1"]["estimated_start_seconds"] == pytest.approx(0.0, abs=1.0)
    assert etas["q2"]["estimated_start_seconds"] > 90.0
    assert etas["q3"]["estimated_start_seconds"] > 90.0
    assert etas["q3"]["jobs_ahead"] == 3


def test_queue_eta_accounts_for_in_flight_remaining():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    running = _job(
        "r1", "running",
        created=_iso(now - timedelta(seconds=120)),
        started=_iso(now - timedelta(seconds=100)),
    )
    queued = _job("q1", "queued", created=_iso(now - timedelta(seconds=5)))
    jobs = [running, queued]
    out = eta.compute_eta(queued, jobs, 2, now=now)
    # C=2: one server busy (~10s remaining), one free now => queued starts ~0s.
    assert out["estimated_start_seconds"] == pytest.approx(0.0, abs=1.0)
    assert out["jobs_ahead"] == 1


def test_single_server_serializes_queue():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    base = now - timedelta(seconds=5)
    jobs = [
        _job(f"q{i}", "queued", created=_iso(base + timedelta(seconds=i)))
        for i in range(3)
    ]
    third = eta.compute_eta(jobs[2], jobs, 1, now=now)
    # C=1: third job waits for two ~110s runs ahead of it.
    assert third["estimated_start_seconds"] > 200.0


def test_record_sample_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("ELB_OPENAPI_ETA_ENABLED", "false")
    importlib.reload(eta)
    eta._store._mem.clear()
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    job = _job(
        "j1", "completed",
        created=_iso(now - timedelta(seconds=120)),
        started=_iso(now - timedelta(seconds=110)),
        completed=_iso(now),
    )
    eta.record_sample(job, [job])
    assert eta._store._mem == {}


def test_record_sample_persists_run_time():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    job = _job(
        "j1", "completed",
        created=_iso(now - timedelta(seconds=120)),
        started=_iso(now - timedelta(seconds=110)),
        completed=_iso(now),
    )
    eta.record_sample(job, [job])
    pk = eta._partition_key("core_nt", eta.bucket_for(1, 500), cold=True)
    # Single isolated job => treated as cold (no prior activity).
    assert pk in eta._store._mem
    assert eta._store._mem[pk]["count"] == 1.0
    assert eta._store._mem[pk]["mean"] == pytest.approx(110.0, abs=1.0)


# ── cross-replica ETag-merge convergence ────────────────────────────────────


class _FakeEntity(dict):
    """Mimic azure.data.tables.TableEntity: a dict carrying ``.metadata``."""

    def __init__(self, data, etag):
        super().__init__(data)
        self.metadata = {"etag": etag}


class _FakeTable:
    """Single shared backend two ``_Store`` instances write through.

    Enforces ETag optimistic concurrency so the test proves two replicas merge
    samples for the same key without clobbering each other.
    """

    def __init__(self):
        self._rows: dict[tuple, dict] = {}
        self._etags: dict[tuple, int] = {}

    def get_entity(self, partition_key, row_key):
        from azure.core.exceptions import ResourceNotFoundError

        key = (partition_key, row_key)
        if key not in self._rows:
            raise ResourceNotFoundError("missing")
        return _FakeEntity(self._rows[key], f"W/{self._etags[key]}")

    def create_entity(self, entity):
        from azure.core.exceptions import HttpResponseError

        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self._rows:
            err = HttpResponseError("conflict")
            err.status_code = 409
            raise err
        self._rows[key] = dict(entity)
        self._etags[key] = 1

    def update_entity(self, entity, mode=None, etag=None, match_condition=None):
        from azure.core.exceptions import ResourceModifiedError

        key = (entity["PartitionKey"], entity["RowKey"])
        current = f"W/{self._etags.get(key)}"
        if etag != current:
            raise ResourceModifiedError("etag mismatch")
        self._rows[key] = dict(entity)
        self._etags[key] += 1


def _seed_store_with_table(table):
    store = eta._Store()
    store._client = table
    store._client_tried = True
    # Force every read to consult the shared table (no local cache hiding peers).
    return store


def test_cross_replica_etag_merge_converges(monkeypatch):
    pytest.importorskip("azure.core")
    monkeypatch.setattr(eta, "_CACHE_TTL_SECONDS", 0)
    table = _FakeTable()
    rep_a = _seed_store_with_table(table)
    rep_b = _seed_store_with_table(table)

    # Two replicas each record one 100s sample for the same key.
    rep_a.update("core_nt|s1.b0|warm", 100.0)
    rep_b.update("core_nt|s1.b0|warm", 100.0)

    # The shared row must reflect BOTH samples (no last-writer-wins clobber).
    row = rep_a.get("core_nt|s1.b0|warm")
    assert row is not None
    assert row["count"] == 2.0
    assert row["mean"] == pytest.approx(100.0, abs=0.1)


def test_update_with_table_creates_then_updates(monkeypatch):
    pytest.importorskip("azure.core")
    monkeypatch.setattr(eta, "_CACHE_TTL_SECONDS", 0)
    table = _FakeTable()
    store = _seed_store_with_table(table)
    store.update("db|b|warm", 50.0)
    store.update("db|b|warm", 70.0)
    row = store.get("db|b|warm")
    assert row is not None
    assert row["count"] == 2.0
    assert row["mean"] == pytest.approx(60.0, abs=1.0)

