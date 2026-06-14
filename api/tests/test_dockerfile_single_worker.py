"""Guard the api sidecar against multi-worker uvicorn startup.

Responsibility: Lock the api container to a single uvicorn worker process
Edit boundaries: Assertion-only; do not import the app or hit the network.
Key entry points: `test_api_dockerfile_runs_single_uvicorn_worker`,
`test_no_bicep_override_reintroduces_multiple_workers`
Risky contracts: The api holds process-local one-shot ticket stores (terminal
WebSocket, BLAST log SSE, sidecar metric/log SSE) that are written by one
request and redeemed by a second connection. With >1 worker the issue and the
redeem land on different processes, so every terminal/SSE upgrade 403s.
Validation: `uv run pytest -q api/tests/test_dockerfile_single_worker.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_DOCKERFILE = _REPO_ROOT / "api" / "Dockerfile"
_CONTROL_BICEP = _REPO_ROOT / "infra" / "modules" / "containerAppControl.bicep"


def _api_dockerfile_text() -> str:
    return _API_DOCKERFILE.read_text(encoding="utf-8")


def test_api_dockerfile_runs_single_uvicorn_worker() -> None:
    """The api CMD must not start more than one uvicorn worker.

    Process-local ticket stores (see module docstring) require the issue and
    redeem of a one-shot ticket to land on the same process. `--workers 2`
    silently broke the browser terminal and every SSE log/metric stream with a
    403 on the upgrade. Keep this at 1.
    """
    text = _api_dockerfile_text()
    match = re.search(r'"--workers",\s*"(\d+)"', text)
    assert match is not None, "api Dockerfile no longer pins --workers explicitly"
    worker_count = int(match.group(1))
    assert worker_count == 1, (
        f"api Dockerfile starts uvicorn with {worker_count} workers; the api "
        "sidecar holds process-local one-shot ticket stores (terminal WS, "
        "BLAST log SSE, sidecar metric/log SSE) that break across workers. "
        "Keep --workers 1; offload CPU-bound work to a thread/Celery instead."
    )


def test_no_bicep_override_reintroduces_multiple_workers() -> None:
    """The api sidecar must not gain a command/args override that re-adds workers.

    The api container in containerAppControl.bicep intentionally relies on the
    Dockerfile CMD. If a future edit adds an explicit uvicorn invocation there,
    it must not smuggle back `--workers <N>` with N > 1.
    """
    text = _CONTROL_BICEP.read_text(encoding="utf-8")
    for match in re.finditer(r"--workers['\"]?\s*[,:]?\s*['\"]?(\d+)", text):
        assert int(match.group(1)) == 1, (
            "containerAppControl.bicep reintroduces a multi-worker uvicorn "
            "invocation for an api-image sidecar; this breaks the process-local "
            "ticket stores. Keep a single worker."
        )
