"""Unit tests for `fetch_pod_all_container_logs` container-error visibility.

Exercises the enrichment that makes container-internal errors visible in the
Workloads "Logs" view: per-container headers, the kubelet waiting reason on a
failed log GET (CrashLoopBackOff / PodInitializing), and the previous-instance
log of a restarted container (where a crash's real output lives).

Responsibility: Verify `fetch_pod_all_container_logs` surfaces crash/previous
output instead of a blank box.
Edit boundaries: Pure unit tests — a fake session, no real K8s API.
Key entry points: the `test_*` functions below.
Risky contracts: The block header `--- container: <name> ... ---` and the
previous-instance fetch (`previous=true`, only when restartCount > 0) are
load-bearing for diagnosis.
Validation: `uv run pytest -q api/tests/test_pod_container_logs.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.k8s.observability import fetch_pod_all_container_logs


class _Resp:
    def __init__(self, *, json_body: Any = None, text: str = "", fail: bool = False) -> None:
        self._json = json_body if json_body is not None else {}
        self.text = text
        self._fail = fail

    def raise_for_status(self) -> None:
        if self._fail:
            raise RuntimeError("HTTP 400")

    def json(self) -> Any:
        return self._json


class _Session:
    """Params-aware fake: distinguishes current vs previous log GETs."""

    def __init__(self, pod_obj: dict[str, Any], log_router: Any) -> None:
        self._pod_obj = pod_obj
        self._log_router = log_router
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: dict[str, Any] | None = None, timeout: Any = None) -> _Resp:
        self.calls.append({"url": url, "params": params or {}})
        if url.endswith("/log"):
            return self._log_router(params or {})
        # pod object GET
        return _Resp(json_body=self._pod_obj)

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def test_previous_instance_log_fetched_for_restarted_container() -> None:
    pod_obj = {
        "spec": {"containers": [{"name": "blastn"}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "blastn",
                    "restartCount": 3,
                    "state": {"running": {"startedAt": "now"}},
                }
            ]
        },
    }

    def router(params: dict[str, Any]) -> _Resp:
        if params.get("previous") == "true":
            return _Resp(text="FATAL: blast db missing (crash output)")
        return _Resp(text="restarted, starting up")

    session = _Session(pod_obj, router)
    out = fetch_pod_all_container_logs(session, "https://k", "blast", "p-1", 200)

    assert "--- container: blastn [restarts=3] ---" in out
    assert "restarted, starting up" in out
    assert "previous instance, restarts=3" in out
    assert "FATAL: blast db missing (crash output)" in out


def test_waiting_reason_surfaced_when_log_unavailable() -> None:
    pod_obj = {
        "spec": {"containers": [{"name": "blastn"}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "blastn",
                    "restartCount": 0,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m restarting failed container",
                        }
                    },
                }
            ]
        },
    }

    def router(_params: dict[str, Any]) -> _Resp:
        # Container is waiting → kubelet 400s the log GET.
        return _Resp(fail=True)

    session = _Session(pod_obj, router)
    out = fetch_pod_all_container_logs(session, "https://k", "blast", "p-1", 200)

    assert "[Waiting (CrashLoopBackOff)]" in out
    assert "back-off 5m restarting failed container" in out


def test_clean_single_container_returns_raw_body() -> None:
    pod_obj = {
        "spec": {"containers": [{"name": "blastn"}]},
        "status": {
            "containerStatuses": [
                {"name": "blastn", "restartCount": 0, "state": {"running": {}}}
            ]
        },
    }

    def router(_params: dict[str, Any]) -> _Resp:
        return _Resp(text="hello output\nsecond line")

    session = _Session(pod_obj, router)
    out = fetch_pod_all_container_logs(session, "https://k", "blast", "p-1", 200)

    # Healthy single container keeps the legacy raw-body shape (no header).
    assert out == "hello output\nsecond line"
    # And no previous fetch is attempted when restartCount == 0.
    assert all(c["params"].get("previous") != "true" for c in session.calls)


def test_terminated_error_single_container_shows_state() -> None:
    pod_obj = {
        "spec": {"containers": [{"name": "blastn"}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "blastn",
                    "restartCount": 0,
                    "state": {
                        "terminated": {
                            "exitCode": 1,
                            "reason": "Error",
                            "message": "segfault in blastn",
                        }
                    },
                }
            ]
        },
    }

    def router(_params: dict[str, Any]) -> _Resp:
        return _Resp(text="")  # terminated container flushed no tail

    session = _Session(pod_obj, router)
    out = fetch_pod_all_container_logs(session, "https://k", "blast", "p-1", 200)

    assert "[Terminated exit 1 (Error)]" in out
    assert "segfault in blastn" in out
