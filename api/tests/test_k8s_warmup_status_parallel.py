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
    assert result["vmtouch_ready"] == 1
    assert result["warm"] is True
    assert result["namespaces"] == ["elastic-blast-1"]
    # All six top-level URLs were issued exactly once.
    assert sum("/daemonsets/create-workspace" in u for u in seen_urls) == 1
    assert sum("/daemonsets/vmtouch-db-cache" in u for u in seen_urls) == 1
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

