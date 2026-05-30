"""STRICT_EXEC_RATE_LIMIT gate for the loopback exec server.

Responsibility: Exercise both the default-OFF and STRICT_EXEC_RATE_LIMIT=true
paths of `terminal/exec_server.py::_rate_limit_check` and `do_POST`, per
§12a Rule 4 (new guards ship default-OFF behind `STRICT_*` env vars and
must include both ON-path and OFF-path coverage).
Edit boundaries: Imports `exec_server` via `importlib.util.spec_from_file_location`
so the terminal sidecar module stays out of `sys.modules['api.*']`. Do not add
calls that depend on a live network or real Azure credentials.
Key entry points: `_load_exec_server`, `test_rate_limit_off_path_default`,
`test_rate_limit_on_path_allows_within_window`,
`test_rate_limit_on_path_blocks_over_cap`,
`test_rate_limit_on_path_per_binary_isolation`,
`test_rate_limit_on_path_sliding_window_recovers`,
`test_rate_limit_http_returns_429_with_retry_after`.
Risky contracts: `_rate_limit_enabled()` reads `STRICT_EXEC_RATE_LIMIT` on
every call; tests use `monkeypatch.setenv` (no module reload needed). All
tests call `_rate_limit_reset_for_tests()` after touching the bucket so
they cannot poison each other.
Validation: `uv run pytest -q api/tests/test_exec_rate_limit.py`.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import socket
import sys
import threading
import time
from collections.abc import Iterator
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest


def _load_exec_server() -> ModuleType:
    """Load `terminal/exec_server.py` as a standalone module.

    Mirrors the pattern in `api/tests/test_persona_matrix.py` so the
    terminal sidecar module never collides with `api.*` imports.
    """
    sys.modules.pop("exec_server", None)
    exec_path = Path(__file__).resolve().parent.parent.parent / "terminal" / "exec_server.py"
    spec = importlib.util.spec_from_file_location("exec_server", exec_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def exec_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    # Tighten the window so tests stay fast.
    monkeypatch.setenv("EXEC_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("EXEC_RATE_LIMIT_PER_WINDOW", "3")
    # Default OFF — individual tests opt in.
    monkeypatch.delenv("STRICT_EXEC_RATE_LIMIT", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    module = _load_exec_server()
    try:
        yield module
    finally:
        module._rate_limit_reset_for_tests()
        sys.modules.pop("exec_server", None)


# ---------------------------------------------------------------------------
# Unit-level tests: _rate_limit_check directly.
# ---------------------------------------------------------------------------


def test_rate_limit_off_path_default(
    monkeypatch: pytest.MonkeyPatch, exec_module: ModuleType
) -> None:
    """OFF path — no gate — must permit unlimited calls (Rule 4 default)."""
    monkeypatch.delenv("STRICT_EXEC_RATE_LIMIT", raising=False)
    for _ in range(50):
        allowed, retry = exec_module._rate_limit_check("kubectl")
        assert allowed is True
        assert retry == 0


def test_rate_limit_on_path_allows_within_window(
    monkeypatch: pytest.MonkeyPatch, exec_module: ModuleType
) -> None:
    """ON path — first N calls inside the per-window cap must succeed."""
    monkeypatch.setenv("STRICT_EXEC_RATE_LIMIT", "true")
    for _ in range(3):
        allowed, retry = exec_module._rate_limit_check("kubectl")
        assert allowed is True
        assert retry == 0


def test_rate_limit_on_path_blocks_over_cap(
    monkeypatch: pytest.MonkeyPatch, exec_module: ModuleType
) -> None:
    """ON path — request N+1 inside the same window must be denied with
    a positive `retry_after_seconds`."""
    monkeypatch.setenv("STRICT_EXEC_RATE_LIMIT", "true")
    for _ in range(3):
        assert exec_module._rate_limit_check("kubectl")[0] is True
    allowed, retry = exec_module._rate_limit_check("kubectl")
    assert allowed is False
    assert retry >= 1
    assert retry <= 60  # bounded by the window


def test_rate_limit_on_path_per_binary_isolation(
    monkeypatch: pytest.MonkeyPatch, exec_module: ModuleType
) -> None:
    """ON path — a hot binary must not starve another binary's quota."""
    monkeypatch.setenv("STRICT_EXEC_RATE_LIMIT", "true")
    for _ in range(3):
        assert exec_module._rate_limit_check("kubectl")[0] is True
    # kubectl bucket is full but azcopy must still have its own headroom.
    for _ in range(3):
        assert exec_module._rate_limit_check("azcopy")[0] is True
    assert exec_module._rate_limit_check("azcopy")[0] is False
    assert exec_module._rate_limit_check("kubectl")[0] is False


def test_rate_limit_on_path_sliding_window_recovers(
    monkeypatch: pytest.MonkeyPatch, exec_module: ModuleType
) -> None:
    """ON path — once entries roll out of the sliding window, new
    requests must be admitted again."""
    monkeypatch.setenv("STRICT_EXEC_RATE_LIMIT", "true")
    # Inject synthetic timestamps so the test is wall-clock independent.
    base = 1_000_000.0
    for offset in (0.0, 0.5, 1.0):
        allowed, _ = exec_module._rate_limit_check("kubectl", now=base + offset)
        assert allowed is True
    # Same instant — 4th request must trip the cap.
    allowed, retry = exec_module._rate_limit_check("kubectl", now=base + 1.0)
    assert allowed is False
    assert retry >= 1
    # Fast-forward past the window — bucket clears, request admitted again.
    allowed, retry = exec_module._rate_limit_check("kubectl", now=base + 70.0)
    assert allowed is True
    assert retry == 0


def test_rate_limit_can_be_flipped_at_runtime(
    monkeypatch: pytest.MonkeyPatch, exec_module: ModuleType
) -> None:
    """`_rate_limit_enabled` reads the env each call, so a midstream flip
    of `STRICT_EXEC_RATE_LIMIT=false` must immediately re-open the gate
    without a module reload. Guards against the regression where the flag
    was evaluated only at import time."""
    monkeypatch.setenv("STRICT_EXEC_RATE_LIMIT", "true")
    for _ in range(3):
        assert exec_module._rate_limit_check("kubectl")[0] is True
    assert exec_module._rate_limit_check("kubectl")[0] is False
    monkeypatch.delenv("STRICT_EXEC_RATE_LIMIT", raising=False)
    # Gate is off — even though the bucket is full, requests are admitted.
    for _ in range(10):
        assert exec_module._rate_limit_check("kubectl")[0] is True


# ---------------------------------------------------------------------------
# HTTP-level test: do_POST returns 429 with Retry-After when limited.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_rate_limit_http_returns_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when STRICT_EXEC_RATE_LIMIT=true and the per-window
    cap is exhausted, /exec must respond 429 with a numeric `Retry-After`
    header and a JSON body that surfaces the same value."""
    port = _free_port()
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("EXEC_TOKEN", token)
    monkeypatch.setenv("EXEC_PORT", str(port))
    monkeypatch.setenv("EXEC_HOST", "127.0.0.1")
    monkeypatch.setenv("EXEC_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("STRICT_EXEC_RATE_LIMIT", "true")
    monkeypatch.setenv("EXEC_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("EXEC_RATE_LIMIT_PER_WINDOW", "2")
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)

    module = _load_exec_server()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), module._Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # Wait until the listener is up.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)

    try:
        # An argv that the allowlist accepts but that exits immediately so
        # the request unwinds fast enough to exhaust the bucket inside the
        # test budget. `kubectl version --client=true` is one of the
        # cheapest allowlisted invocations.
        body = json.dumps(
            {
                "argv": ["kubectl", "version", "--client=true", "--output=json"],
                "timeout": 5,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Exec-Token": token,
        }

        def _post() -> tuple[int, dict[str, str], dict[str, object]]:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("POST", "/exec", body=body, headers=headers)
                resp = conn.getresponse()
                payload_bytes = resp.read()
                hdrs = {k: v for k, v in resp.getheaders()}
                payload: dict[str, object]
                try:
                    payload = json.loads(payload_bytes.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                return resp.status, hdrs, payload
            finally:
                conn.close()

        # Two requests within the cap — kubectl may not be installed in CI,
        # so we accept either 200 (success) OR 500 (subprocess failure).
        # Both consume a token from the bucket, which is what we are
        # actually testing.
        for _ in range(2):
            status, _, _ = _post()
            assert status in (200, 500), f"unexpected status {status} for permitted call"

        # Third request — bucket exhausted — must be 429 with Retry-After.
        status, hdrs, payload = _post()
        assert status == 429
        retry_after_header = hdrs.get("Retry-After") or hdrs.get("retry-after")
        assert retry_after_header is not None
        assert int(retry_after_header) >= 1
        assert payload.get("error") == "exec server rate-limited"
        assert payload.get("binary") == "kubectl"
        assert isinstance(payload.get("retry_after_seconds"), int)
        assert payload["retry_after_seconds"] >= 1  # type: ignore[operator]
    finally:
        httpd.shutdown()
        httpd.server_close()
        module._rate_limit_reset_for_tests()
        sys.modules.pop("exec_server", None)
