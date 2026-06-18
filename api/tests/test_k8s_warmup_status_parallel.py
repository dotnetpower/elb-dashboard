"""Tests for the parallelised `k8s_warmup_status` fan-out.

Responsibility: Confirm that the ThreadPoolExecutor fan-out used to speed
up `k8s_warmup_status` issues every expected Kubernetes API call and
assembles the response identically to the previous sequential
implementation.
Edit boundaries: Keep mocks scoped to the requests session — do not exercise
real Kubernetes or Azure paths.
Key entry points: `test_warmup_status_issues_all_expected_calls`,
`test_warmup_status_handles_missing_workloads_gracefully`,
`test_warmup_status_parallel_pod_logs`
Risky contracts: Do not assume a specific call ordering — only assert that
the set of called URLs matches the expected workload reads.
Validation: `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py`.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from api.services.k8s import monitoring as km


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


def _fake_session(handler) -> Any:
    session = MagicMock()
    session.get = MagicMock(side_effect=handler)
    session.close = MagicMock()
    return session


def test_warmup_status_issues_all_expected_calls() -> None:
    seen_urls: list[str] = []
    lock = threading.Lock()

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        with lock:
            seen_urls.append(url)
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 1, "desiredNumberScheduled": 2}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 1}})
        if "/batch/v1/namespaces/default/jobs" in url:
            # Both setup and warmup label selectors currently return an
            # empty job list — phase-2 (pod logs, node check) is exercised
            # by `test_warmup_status_parallel_pod_logs`.
            return _FakeResponse({"items": []})
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse(
                {
                    "items": [
                        {"metadata": {"name": "elastic-blast-1"}},
                        {"metadata": {"name": "other"}},
                    ]
                }
            )
        if "/pods" in url:
            return _FakeResponse({"items": []})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    # Result merges the parallel reads identically to the sequential version.
    assert result["workspace_ready"] == 1
    assert result["workspace_desired"] == 2
    # The legacy vmtouch-db-cache DaemonSet is no longer probed (it was removed
    # when warmup moved to a Job-based model), so vmtouch_ready stays 0 and the
    # URL is never requested. `warm` still resolves from the create-workspace
    # DaemonSet's ready count.
    assert result["vmtouch_ready"] == 0
    assert result["warm"] is True
    assert result["namespaces"] == ["elastic-blast-1"]
    # The five remaining top-level URLs were issued exactly once each; the
    # dead vmtouch-db-cache probe is gone.
    assert sum("/daemonsets/create-workspace" in u for u in seen_urls) == 1
    assert sum("/daemonsets/vmtouch-db-cache" in u for u in seen_urls) == 0
    assert sum("/batch/v1/namespaces/default/jobs" in u for u in seen_urls) == 2  # setup + warmup
    assert sum(u.endswith("/apps/v1/namespaces/default/daemonsets") for u in seen_urls) == 1
    assert sum(u.endswith("/api/v1/namespaces") for u in seen_urls) == 1


def test_warmup_status_handles_missing_workloads_gracefully() -> None:
    """Non-200 responses on any leg must not break the merge — the result
    falls back to the empty defaults."""

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    assert result["warm"] is False
    assert result["workspace_ready"] == 0
    assert result["vmtouch_ready"] == 0
    assert result["databases"] == []
    assert result["namespaces"] == []


def test_warmup_status_parallel_pod_logs() -> None:
    """The pod-log fan-out must read every pod's log even when each fetch
    sleeps briefly. With 4 pods and 50ms per fetch, wall time stays under
    150ms (4x faster than serial)."""

    pods = [{"metadata": {"name": f"warmup-pod-{i}"}} for i in range(4)]
    warmup_jobs_payload = {"items": []}

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 0, "desiredNumberScheduled": 0}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 0}})
        if "/batch/v1/namespaces/default/jobs" in url:
            label = (params or {}).get("labelSelector", "")
            if "setup" in label:
                return _FakeResponse({"items": []})
            # warmup jobs — return one stub job so phase-2 triggers
            return _FakeResponse(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "warmup-stub",
                                "labels": {"db": "nt"},
                            },
                            "status": {},
                            "spec": {"template": {"spec": {"containers": []}}},
                        }
                    ]
                }
                if "db-warmup" in label
                else warmup_jobs_payload
            )
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse({"items": []})
        if "/api/v1/namespaces/default/pods" in url and "/log" not in url:
            return _FakeResponse({"items": pods})
        if url.endswith("/api/v1/nodes"):
            return _FakeResponse({"items": []})
        if "/log" in url:
            time.sleep(0.05)  # 50 ms each — serial would be ~200 ms.
            return _FakeResponse({}, status_code=200, text="log content")
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    start = time.monotonic()
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")
    elapsed = time.monotonic() - start
    # 4 pod logs @ 50ms in parallel should finish well under 200ms.
    # Generous bound to avoid flakes on slow CI.
    assert elapsed < 0.4, f"parallel pod logs took {elapsed:.3f}s, expected <0.4s"


def test_warmup_status_tags_setup_only_dbs_with_setup_source() -> None:
    """A DB seen only via an `init-ssd-*` setup Job (e.g. from a prior BLAST
    submit) must be tagged `sources=["setup"]` so the New Search run-profile
    picker does not auto-flip to "Warmed database".
    """

    setup_job = {
        "metadata": {"name": "init-ssd-abc123-0"},
        "status": {"succeeded": 1, "failed": 0, "active": 0},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "ELB_DB", "value": "16S_ribosomal_RNA"},
                                {"name": "ELB_DB_MOL_TYPE", "value": "nucl"},
                            ]
                        }
                    ]
                }
            }
        },
    }

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 0, "desiredNumberScheduled": 0}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 0}})
        if "/batch/v1/namespaces/default/jobs" in url:
            label = (params or {}).get("labelSelector", "")
            if "setup" in label:
                return _FakeResponse({"items": [setup_job]})
            return _FakeResponse({"items": []})
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse({"items": []})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    dbs = {db["name"]: db for db in result["databases"]}
    assert "16S_ribosomal_RNA" in dbs
    assert dbs["16S_ribosomal_RNA"]["status"] == "Ready"
    assert dbs["16S_ribosomal_RNA"]["sources"] == ["setup"]


def test_warmup_status_merges_setup_and_warmup_sources() -> None:
    """When both an `init-ssd-*` setup Job and an `app=db-warmup` Job exist
    for the same DB, the merged entry unions both source tags so the
    frontend still treats the DB as explicitly warmed.
    """

    setup_job = {
        "metadata": {"name": "init-ssd-deadbeef-0"},
        "status": {"succeeded": 1},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"env": [{"name": "ELB_DB", "value": "core_nt"}]}
                    ]
                }
            }
        },
    }
    warmup_job = {
        "metadata": {"labels": {"db": "core_nt", "shard": "00"}},
        "status": {"succeeded": 1},
    }

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 0, "desiredNumberScheduled": 0}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 0}})
        if "/batch/v1/namespaces/default/jobs" in url:
            label = (params or {}).get("labelSelector", "")
            if "setup" in label:
                return _FakeResponse({"items": [setup_job]})
            if "db-warmup" in label:
                return _FakeResponse({"items": [warmup_job]})
            return _FakeResponse({"items": []})
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse({"items": []})
        if "/api/v1/namespaces/default/pods" in url and "/log" not in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/nodes"):
            return _FakeResponse({"items": []})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    dbs = {db["name"]: db for db in result["databases"]}
    assert sorted(dbs["core_nt"].get("sources", [])) == ["setup", "warmup"]


def test_warmup_status_warmup_jobs_are_authoritative_denominator() -> None:
    """The node-local warmup-Job count (one per Ready node) must win over the
    ElasticBLAST `init-ssd-*` setup-Job count.

    ElasticBLAST splits core_nt into more shards than there are nodes (e.g. 20
    `init-ssd-*` setup Jobs for a 10-node cluster). A `max` merge with those
    setup Jobs used to inflate the dashboard denominator to "10/20" and hold
    BLAST submit at `warmup_not_ready`. With 10 node-local warmup Jobs all
    succeeded the merged entry must report 10/10 Ready, not 10/20 Loading.
    """

    setup_jobs = [
        {
            "metadata": {"name": f"init-ssd-deadbeef-{idx}"},
            # 10 setup Jobs done, 10 still active -> 20 total / 10 ready.
            "status": {"succeeded": 1} if idx < 10 else {"active": 1},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"env": [{"name": "ELB_DB", "value": f"core_nt_shard_{idx:02d}"}]}
                        ]
                    }
                }
            },
        }
        for idx in range(20)
    ]
    warmup_jobs = [
        {
            "metadata": {"labels": {"db": "core_nt", "shard": f"{idx:02d}"}},
            "status": {"succeeded": 1},
        }
        for idx in range(10)
    ]

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 0, "desiredNumberScheduled": 0}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 0}})
        if "/batch/v1/namespaces/default/jobs" in url:
            label = (params or {}).get("labelSelector", "")
            if "setup" in label:
                return _FakeResponse({"items": setup_jobs})
            if "db-warmup" in label:
                return _FakeResponse({"items": warmup_jobs})
            return _FakeResponse({"items": []})
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse({"items": []})
        if "/api/v1/namespaces/default/pods" in url and "/log" not in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/nodes"):
            return _FakeResponse({"items": []})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    core = {db["name"]: db for db in result["databases"]}["core_nt"]
    assert core["total_jobs"] == 10, core
    assert core["nodes_ready"] == 10, core
    assert core["nodes_active"] == 0, core
    assert core["status"] == "Ready", core
    assert sorted(core.get("sources", [])) == ["setup", "warmup"]


def test_warmup_status_merge_carries_source_version_over_setup_entry() -> None:
    """The warmup DB-generation marker must survive a merge onto a
    pre-existing setup entry.

    `result["databases"]` is seeded from `init-ssd-*` setup Jobs first (these
    carry NO `elb.dashboard/source-version` annotation), then the node-local
    warmup Jobs (which DO carry it) are merged in. If the merge drops the
    marker, the final entry is `status="Ready"` but marker-less, and the BLAST
    submit gate (`ensure_node_warmup_ready_for_submit`) fails with
    "node warmup for core_nt has no DB generation marker" even though the
    dashboard card shows the DB as warm. This is a regression guard for that.
    """

    setup_jobs = [
        {
            "metadata": {"name": f"init-ssd-cafe-{idx}"},
            "status": {"succeeded": 1},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"env": [{"name": "ELB_DB", "value": f"core_nt_shard_{idx:02d}"}]}
                        ]
                    }
                }
            },
        }
        for idx in range(10)
    ]
    warmup_jobs = [
        {
            "metadata": {
                "labels": {"db": "core_nt", "shard": f"{idx:02d}"},
                "annotations": {"elb.dashboard/source-version": "2026-05-26-01-05-01"},
            },
            "status": {"succeeded": 1},
        }
        for idx in range(10)
    ]

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 0, "desiredNumberScheduled": 0}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 0}})
        if "/batch/v1/namespaces/default/jobs" in url:
            label = (params or {}).get("labelSelector", "")
            if "setup" in label:
                return _FakeResponse({"items": setup_jobs})
            if "db-warmup" in label:
                return _FakeResponse({"items": warmup_jobs})
            return _FakeResponse({"items": []})
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse({"items": []})
        if "/api/v1/namespaces/default/pods" in url and "/log" not in url:
            return _FakeResponse({"items": []})
        if url.endswith("/api/v1/nodes"):
            return _FakeResponse({"items": []})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    core = {db["name"]: db for db in result["databases"]}["core_nt"]
    assert core["status"] == "Ready", core
    assert core.get("source_version") == "2026-05-26-01-05-01", core
    assert core.get("source_versions") == ["2026-05-26-01-05-01"], core


def test_warmup_status_daemonset_tags_warmup_source() -> None:
    """`app=db-warmup` DaemonSets must also tag their entries `warmup`."""

    daemonset = {
        "metadata": {"labels": {"db": "nt", "app": "db-warmup"}},
        "status": {"desiredNumberScheduled": 2, "numberReady": 2},
    }

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if url.endswith("/daemonsets/create-workspace"):
            return _FakeResponse({"status": {"numberReady": 0, "desiredNumberScheduled": 0}})
        if url.endswith("/daemonsets/vmtouch-db-cache"):
            return _FakeResponse({"status": {"numberReady": 0}})
        if "/batch/v1/namespaces/default/jobs" in url:
            return _FakeResponse({"items": []})
        if "/apps/v1/namespaces/default/daemonsets" in url:
            return _FakeResponse({"items": [daemonset]})
        if url.endswith("/api/v1/namespaces"):
            return _FakeResponse({"items": []})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)
    with patch.object(km, "_get_k8s_session", return_value=(session, "https://k8s")):
        result = km.k8s_warmup_status(MagicMock(), "sub", "rg", "aks")

    dbs = {db["name"]: db for db in result["databases"]}
    assert dbs["nt"]["sources"] == ["warmup"]


def test_warmup_pod_log_connection_abort_degrades_without_exception() -> None:
    """A ``ConnectionError(RemoteDisconnected)`` on the best-effort pod-log GET
    must degrade to an empty log (issue #45) and must run inside
    ``suppress_dependency_telemetry`` so OTel records no App Insights
    dependency exception for the transient abort.
    """
    import requests
    from api.services.k8s import warmup_status as ws
    from urllib3.exceptions import ProtocolError

    pods = [{"metadata": {"name": f"warm-core-nt-{i}"}} for i in range(3)]
    abort = requests.exceptions.ConnectionError(
        ProtocolError(
            "Connection aborted.",
            requests.exceptions.ConnectionError("Remote end closed connection"),
        )
    )

    def handler(url: str, params=None, timeout=10):  # type: ignore[no-untyped-def]
        if "/log" in url:
            raise abort
        if "/api/v1/namespaces/default/pods" in url:
            return _FakeResponse({"items": pods})
        return _FakeResponse({}, status_code=404)

    session = _fake_session(handler)

    enter_count = {"n": 0}

    class _CountingCM:
        def __enter__(self) -> None:
            enter_count["n"] += 1

        def __exit__(self, *exc: Any) -> bool:
            return False

    with patch.object(ws, "suppress_dependency_telemetry", lambda: _CountingCM()):
        pods_out, logs = ws._warmup_pods_and_logs(session, "https://k8s")

    # No exception propagated; every pod degraded to "no log".
    assert pods_out == pods
    assert logs == {}
    # The suppression context wrapped every pod-log GET (one per pod).
    assert enter_count["n"] == len(pods)


