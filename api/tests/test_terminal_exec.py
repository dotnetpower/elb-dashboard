"""End-to-end tests for the api ↔ terminal exec channel.

Responsibility: End-to-end tests for the api ↔ terminal exec channel
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_free_port`, `exec_server`, `test_healthz_works_without_auth`,
`test_run_rejects_when_token_missing`, `test_run_rejects_when_token_wrong`,
`test_run_rejects_argv_outside_allowlist`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_exec.py`.
"""

from __future__ import annotations

import importlib
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TERMINAL_DIR = REPO_ROOT / "terminal"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def exec_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """Boot terminal/exec_server.py in a background thread.

    Returns ``(upstream_url, token)``. Server is shut down at fixture
    teardown.
    """
    port = _free_port()
    token = secrets.token_urlsafe(32)

    monkeypatch.setenv("EXEC_TOKEN", token)
    monkeypatch.setenv("EXEC_PORT", str(port))
    monkeypatch.setenv("EXEC_HOST", "127.0.0.1")
    monkeypatch.setenv("EXEC_MAX_CONCURRENCY", "2")

    sys.path.insert(0, str(TERMINAL_DIR))
    if "exec_server" in sys.modules:
        del sys.modules["exec_server"]
    exec_module = importlib.import_module("exec_server")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), exec_module._Handler)
    httpd.daemon_threads = True
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # Wait for the listener to be ready (max 1 s).
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)

    upstream = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("TERMINAL_EXEC_UPSTREAM", upstream)

    yield upstream, token

    httpd.shutdown()
    httpd.server_close()
    sys.path.remove(str(TERMINAL_DIR))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def test_healthz_works_without_auth(exec_server: tuple[str, str]) -> None:
    from api.services import terminal_exec

    body = terminal_exec.healthz()
    assert body["status"] == "ok"
    assert body["max_concurrency"] == 2


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_run_rejects_when_token_missing(
    monkeypatch: pytest.MonkeyPatch, exec_server: tuple[str, str]
) -> None:
    monkeypatch.delenv("EXEC_TOKEN", raising=False)
    from api.services import terminal_exec

    with pytest.raises(terminal_exec.TerminalExecError, match="EXEC_TOKEN"):
        terminal_exec.run(["az", "version"])


def test_run_rejects_when_token_wrong(
    monkeypatch: pytest.MonkeyPatch, exec_server: tuple[str, str]
) -> None:
    monkeypatch.setenv("EXEC_TOKEN", "wrong-token-but-long-enough-to-pass-startup")
    from api.services import terminal_exec

    with pytest.raises(terminal_exec.TerminalExecError, match="401"):
        terminal_exec.run(["az", "version"])


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def test_run_rejects_argv_outside_allowlist(exec_server: tuple[str, str]) -> None:
    from api.services import terminal_exec

    with pytest.raises(terminal_exec.TerminalExecError, match="403"):
        terminal_exec.run(["rm", "-rf", "/"])


def test_run_rejects_argv_with_path(exec_server: tuple[str, str]) -> None:
    from api.services import terminal_exec

    with pytest.raises(terminal_exec.TerminalExecError):
        terminal_exec.run(["/bin/sh", "-c", "echo hi"])


# ---------------------------------------------------------------------------
# Buffered run() — use `kubectl` because it's in the allowlist and `--help`
# always works without a kubeconfig. (`az --version` requires the actual `az`
# binary; `kubectl --help` is more universally available; but neither is
# guaranteed in the test env. Substitute `az` with a stub binary so we test
# the wire contract, not the real azure-cli.)
# ---------------------------------------------------------------------------
def _install_stub(tmp_path: Path, name: str, body: str) -> Path:
    stub = tmp_path / name
    stub.write_text(body)
    stub.chmod(0o755)
    return stub


def test_run_executes_command_and_returns_dict(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replace `az` on PATH with a stub that prints to stdout/stderr."""
    _install_stub(
        tmp_path,
        "az",
        '#!/bin/bash\necho "stdout-line-1"\necho "stdout-line-2"\necho "stderr-line" >&2\nexit 0\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    result = terminal_exec.run(["az", "version"])
    assert result["exit_code"] == 0
    assert "stdout-line-1" in result["stdout"]
    assert "stdout-line-2" in result["stdout"]
    assert "stderr-line" in result["stderr"]
    assert result["timed_out"] is False
    assert result["duration_ms"] >= 0


def test_run_propagates_non_zero_exit(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub(tmp_path, "az", '#!/bin/bash\necho "boom" >&2\nexit 7\n')
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    result = terminal_exec.run(["az", "broken"])
    # Non-zero exit is NOT an exception — it surfaces in the dict.
    assert result["exit_code"] == 7
    assert "boom" in result["stderr"]


def test_run_sanitises_stdout(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Confirm SAS-style URL is redacted in the returned stdout."""
    _install_stub(
        tmp_path,
        "az",
        '#!/bin/bash\necho "https://acct.blob.core.windows.net/c/b?sv=2024&sig=ABC&se=2030"\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    result = terminal_exec.run(["az", "show"])
    assert "sig=ABC" not in result["stdout"]
    assert "<sas-redacted>" in result["stdout"]


def test_run_times_out_and_returns_124(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub(tmp_path, "az", "#!/bin/bash\nsleep 5\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    result = terminal_exec.run(["az", "slow"], timeout_seconds=1)
    assert result["timed_out"] is True
    assert result["exit_code"] == 124


def test_run_writes_stdin_file_before_exec(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub(tmp_path, "az", '#!/bin/bash\ncat "$1"\n')
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    result = terminal_exec.run(
        ["az", "elastic-blast.ini"],
        stdin="[blast]\nqueries=x\n",
        stdin_file="elastic-blast.ini",
    )
    assert result["exit_code"] == 0
    assert "queries=x" in result["stdout"]


def test_run_rejects_unsafe_stdin_file(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.services import terminal_exec

    with pytest.raises(terminal_exec.TerminalExecError, match="400"):
        terminal_exec.run(["az", "version"], stdin="x", stdin_file="../cfg.ini")


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
def test_stream_yields_lines_and_summary(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub(
        tmp_path,
        "az",
        '#!/bin/bash\nfor i in 1 2 3; do echo "line-$i"; done\necho "warn" >&2\nexit 0\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    items = list(terminal_exec.stream(["az", "stream"], timeout_seconds=10))

    # Last item is always the summary.
    assert items, "stream produced no items"
    summary = items[-1]
    assert "exit_code" in summary
    assert summary["exit_code"] == 0

    # Body items are stdout/stderr lines.
    body = items[:-1]
    stdout_lines = [d["line"] for d in body if d.get("stream") == "stdout"]
    stderr_lines = [d["line"] for d in body if d.get("stream") == "stderr"]
    assert "line-1" in stdout_lines
    assert "line-3" in stdout_lines
    assert "warn" in stderr_lines


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------
def test_concurrency_cap_returns_503(
    exec_server: tuple[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With max_concurrency=2, a third concurrent request must get 503."""
    _install_stub(tmp_path, "az", "#!/bin/bash\nsleep 1\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    from api.services import terminal_exec

    results: list[str] = []
    errors: list[Exception] = []

    def _call(idx: int) -> None:
        try:
            terminal_exec.run(["az", str(idx)], timeout_seconds=5)
            results.append("ok")
        except Exception as exc:
            errors.append(exc)
            results.append("err")

    threads = [threading.Thread(target=_call, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # The first two land in the semaphore; the third must be rejected with 503.
    assert results.count("ok") == 2
    assert results.count("err") == 1
    assert any("503" in str(e) for e in errors), errors
