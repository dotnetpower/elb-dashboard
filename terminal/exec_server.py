#!/usr/bin/env python3
"""Loopback exec server for the terminal sidecar.

Responsibility: Loopback exec server for the terminal sidecar
Edit boundaries: Keep terminal-side behavior here; api/worker callers should use service
wrappers.
Key entry points: `_audit`, `_check_token`, `_validate_argv`, `main`
Risky contracts: Bind loopback only, require `X-Exec-Token`, use `shell=False`, and kill process
groups on timeout.
Validation: `uv run pytest -q api/tests/test_terminal_toolchain.py
api/tests/test_terminal_command_guard.py`.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets as _secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ALLOWED_BIN: frozenset[str] = frozenset({"azcopy", "kubectl", "elastic-blast", "elb", "az"})
LISTEN_HOST = os.environ.get("EXEC_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("EXEC_PORT", "7682"))
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")
MAX_CONCURRENCY = int(os.environ.get("EXEC_MAX_CONCURRENCY", "4"))
MAX_BODY_BYTES = int(os.environ.get("EXEC_MAX_BODY_BYTES", str(64 * 1024)))
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 1800  # hard 30-min cap; longer tasks must be split
SIGTERM_GRACE_SECONDS = 5
EXEC_TMP_ROOT = "/tmp/exec"  # noqa: S108 - intentional; cleaned per-request
EXEC_AZURE_CONFIG_DIR = os.environ.get("EXEC_AZURE_CONFIG_DIR", "")
ELB_RUNTIME_OVERRIDES_DIR = os.environ.get(
    "ELB_RUNTIME_OVERRIDES_DIR", "/opt/elb/runtime_overrides"
)
ELB_SOURCE_DIR = os.environ.get("ELB_SOURCE_DIR", "/opt/elb/elastic-blast-azure/src")

LOGGER = logging.getLogger("exec_server")
_semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _audit(event: str, **fields: object) -> None:
    """One-line JSON audit record to stderr (captured by Container Apps logs).

    Never includes argv beyond argv[0], never includes stdin / stdout /
    stderr bodies, never includes the token.
    """
    record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


def _check_token(header_value: str | None) -> bool:
    if not EXEC_TOKEN or not header_value:
        return False
    return hmac.compare_digest(header_value, EXEC_TOKEN)


def _validate_argv(argv: object) -> tuple[list[str] | None, str | None]:
    if not isinstance(argv, list) or not argv:
        return None, "argv must be a non-empty list"
    if not all(isinstance(a, str) for a in argv):
        return None, "argv must be a list of strings"
    bin_name = argv[0]
    if "/" in bin_name or "\\" in bin_name or bin_name.startswith("."):
        return None, f"argv[0] must be a bare binary name, got: {bin_name!r}"
    if bin_name not in ALLOWED_BIN:
        return None, f"argv[0] {bin_name!r} not in allowlist {sorted(ALLOWED_BIN)}"
    return argv, None


def _validate_request(body: bytes) -> tuple[dict | None, int, str | None]:
    if len(body) > MAX_BODY_BYTES:
        return None, 413, f"request body exceeds {MAX_BODY_BYTES} bytes"
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        return None, 400, f"invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, 400, "body must be a JSON object"

    argv, argv_err = _validate_argv(payload.get("argv"))
    if argv_err is not None:
        return None, 403 if "allowlist" in argv_err else 400, argv_err

    stdin_value = payload.get("stdin")
    if stdin_value is not None and not isinstance(stdin_value, str):
        return None, 400, "stdin must be a string or omitted"

    stdin_file = payload.get("stdin_file")
    if stdin_file is not None:
        if not isinstance(stdin_file, str) or not stdin_file:
            return None, 400, "stdin_file must be a relative path string or omitted"
        stdin_path = PurePosixPath(stdin_file)
        if stdin_path.is_absolute() or any(part in {"", ".", ".."} for part in stdin_path.parts):
            return None, 400, "stdin_file must be a relative path without '.' or '..'"
        if stdin_value is None:
            return None, 400, "stdin_file requires stdin"

    cwd = payload.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return None, 400, "cwd must be an absolute path or null"

    timeout = payload.get("timeout_seconds", DEFAULT_TIMEOUT)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return None, 400, "timeout_seconds must be an integer"
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    return (
        {
            "argv": argv,
            "stdin": stdin_value,
            "stdin_file": stdin_file,
            "cwd": cwd,
            "timeout": timeout,
        },
        200,
        None,
    )


def _make_cwd(explicit: str | None) -> tuple[str, bool]:
    """Return (cwd, owned) where `owned` means we created it and must clean up."""
    if explicit:
        return explicit, False
    os.makedirs(EXEC_TMP_ROOT, exist_ok=True)
    path = tempfile.mkdtemp(prefix=f"req-{uuid.uuid4().hex[:8]}-", dir=EXEC_TMP_ROOT)
    return path, True


def _write_stdin_file(req: dict, cwd: str) -> None:
    stdin_file = req.get("stdin_file")
    if not stdin_file:
        return
    path = os.path.join(cwd, stdin_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(req["stdin"])


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    if EXEC_AZURE_CONFIG_DIR and not env.get("AZURE_CONFIG_DIR"):
        env["AZURE_CONFIG_DIR"] = EXEC_AZURE_CONFIG_DIR
    env.setdefault("AZCOPY_AUTO_LOGIN_TYPE", "AZCLI")
    python_path_prefix = [
        path for path in (ELB_RUNTIME_OVERRIDES_DIR, ELB_SOURCE_DIR) if os.path.isdir(path)
    ]
    if python_path_prefix:
        existing = env.get("PYTHONPATH")
        existing_parts = existing.split(os.pathsep) if existing else []
        merged = [*python_path_prefix, *existing_parts]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(part for part in merged if part))
    env.setdefault("ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP", "1")
    env.setdefault("ELB_DASHBOARD_FAST_AZURE_IO", "1")
    return env


def _spawn(argv: list[str], cwd: str, stdin_text: str | None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - argv allowlisted, shell=False
        argv,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=_child_env(),
        # New session so we can SIGKILL the entire process group on timeout.
        start_new_session=True,
        bufsize=0,
    )


def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=SIGTERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_buffered(req: dict) -> dict:
    """Run the command, capture full output, return as a single dict."""
    cwd, owned = _make_cwd(req["cwd"])
    started = time.monotonic()
    try:
        _write_stdin_file(req, cwd)
        proc = _spawn(req["argv"], cwd, req["stdin"])
        try:
            stdin_bytes = req["stdin"].encode("utf-8") if req["stdin"] is not None else None
            out, err = proc.communicate(input=stdin_bytes, timeout=req["timeout"])
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_proc(proc)
            out, err = proc.communicate()
            timed_out = True
        return {
            "exit_code": proc.returncode if not timed_out else 124,
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": err.decode("utf-8", errors="replace"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "timed_out": timed_out,
        }
    finally:
        if owned:
            shutil.rmtree(cwd, ignore_errors=True)


def _stream(req: dict, write_line, client_alive=None) -> dict:
    """Run the command, write JSON Lines via `write_line(dict)`. Returns summary dict.

    ``client_alive`` is an optional callable returning ``True`` while the HTTP
    client is still reachable; once it returns ``False`` the subprocess is
    killed early so a disconnected client cannot keep work running for the
    full ``timeout_seconds``. The supervisor process group is SIGTERM'd then
    SIGKILL'd via ``_kill_proc``.
    """
    cwd, owned = _make_cwd(req["cwd"])
    started = time.monotonic()
    timed_out = [False]
    client_gone = [False]

    def _read_pipe(pipe, stream_name: str) -> None:
        for raw in iter(pipe.readline, b""):
            try:
                decoded = raw.decode("utf-8", errors="replace").rstrip("\n")
                write_line({"stream": stream_name, "line": decoded})
            except (BrokenPipeError, ConnectionResetError):
                # Client gone — flag the main loop and stop trying to write.
                client_gone[0] = True
                return
        try:
            pipe.close()
        except Exception:  # noqa: S110 - pipe close races with subprocess exit; nothing to log
            pass

    try:
        _write_stdin_file(req, cwd)
        proc = _spawn(req["argv"], cwd, req["stdin"])
        if req["stdin"] is not None and proc.stdin is not None:
            try:
                proc.stdin.write(req["stdin"].encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threads = []
        if proc.stdout is not None:
            t_out = threading.Thread(target=_read_pipe, args=(proc.stdout, "stdout"), daemon=True)
            t_out.start()
            threads.append(t_out)
        if proc.stderr is not None:
            t_err = threading.Thread(target=_read_pipe, args=(proc.stderr, "stderr"), daemon=True)
            t_err.start()
            threads.append(t_err)

        deadline = started + req["timeout"]
        client_aborted = False
        while proc.poll() is None:
            if time.monotonic() > deadline:
                _kill_proc(proc)
                timed_out[0] = True
                break
            if client_gone[0] or (client_alive is not None and not client_alive()):
                _kill_proc(proc)
                client_aborted = True
                break
            time.sleep(0.05)

        for t in threads:
            t.join(timeout=2.0)

        return {
            "exit_code": proc.returncode if not timed_out[0] else 124,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "timed_out": timed_out[0],
            "client_aborted": client_aborted,
        }
    finally:
        if owned:
            shutil.rmtree(cwd, ignore_errors=True)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "elb-exec/1.0"
    sys_version = ""  # don't leak python version
    # Per-request socket timeout for HEADER + BODY read.
    #
    # Without this, ``BaseHTTPRequestHandler.timeout`` is ``None`` and a
    # slowloris client (claims Content-Length: 64000, dribbles 1 byte every
    # 60 s) can hold one of the BoundedSemaphore slots forever — four such
    # connections starve the channel.
    #
    # We bump the socket timeout to ``timeout_seconds + slack`` AFTER body
    # read in ``do_POST`` so streaming responses are not killed mid-flight.
    timeout = 15

    def log_message(self, format, *args) -> None:
        # Replace stdlib's stderr access-log; we emit structured audits instead.
        return

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _relax_socket_for_long_response(self, exec_seconds: int) -> None:
        """Extend the per-request socket timeout once we know we're about to
        run a long subprocess. Header / body read already finished by this
        point, so the tight slowloris timeout above is no longer relevant.
        """
        try:
            new_timeout = float(exec_seconds + SIGTERM_GRACE_SECONDS + 30)
            self.connection.settimeout(new_timeout)
        except OSError:
            # Best-effort; if the socket is gone the next write will fail anyway.
            pass

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": f"body too large (max {MAX_BODY_BYTES})"})
            return None
        return self.rfile.read(length)

    def _require_token(self) -> bool:
        if _check_token(self.headers.get("X-Exec-Token")):
            return True
        self._send_json(401, {"error": "missing or invalid X-Exec-Token"})
        return False

    # --- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "max_concurrency": MAX_CONCURRENCY})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in ("/exec", "/exec/stream"):
            self._send_json(404, {"error": "not found"})
            return
        if not self._require_token():
            return
        body = self._read_body()
        if body is None:
            return
        req, status, err = _validate_request(body)
        if req is None:
            assert err is not None
            self._send_json(status, {"error": err})
            return

        # Cap concurrency. We do NOT block forever — quickly reject when busy
        # so the api caller can choose to retry / queue at its layer.
        if not _semaphore.acquire(blocking=False):
            self._send_json(503, {"error": "exec server busy", "max_concurrency": MAX_CONCURRENCY})
            return

        # Header + body read are done; relax the per-request socket timeout so
        # the long subprocess + (optional) NDJSON streaming response are not
        # killed by the slowloris-protection timeout configured on the class.
        self._relax_socket_for_long_response(req["timeout"])

        request_id = uuid.uuid4().hex[:12]
        _audit(
            "exec_start",
            request_id=request_id,
            bin=req["argv"][0],
            argc=len(req["argv"]),
            timeout=req["timeout"],
            stream=(self.path == "/exec/stream"),
        )
        try:
            if self.path == "/exec":
                summary = _run_buffered(req)
                _audit(
                    "exec_done",
                    request_id=request_id,
                    bin=req["argv"][0],
                    exit_code=summary["exit_code"],
                    duration_ms=summary["duration_ms"],
                    timed_out=summary["timed_out"],
                )
                self._send_json(200, summary)
            else:
                # JSON Lines streaming. Connection: close, no Content-Length;
                # client reads until EOF.
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()

                # Track whether the client is still reading. _stream polls
                # this between subprocess output bytes so a client disconnect
                # kills the subprocess early instead of letting it run for
                # the full timeout_seconds.
                client_alive = [True]

                def _write_line(obj: dict) -> None:
                    line = (json.dumps(obj) + "\n").encode("utf-8")
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        client_alive[0] = False
                        # Do not re-raise: _stream's _read_pipe also catches
                        # this and flips its own client_gone flag.

                summary = _stream(req, _write_line, client_alive=lambda: client_alive[0])
                _write_line(summary)
                _audit(
                    "exec_done",
                    request_id=request_id,
                    bin=req["argv"][0],
                    exit_code=summary["exit_code"],
                    duration_ms=summary["duration_ms"],
                    timed_out=summary["timed_out"],
                    client_aborted=summary.get("client_aborted", False),
                )
        except Exception as exc:
            _audit("exec_error", request_id=request_id, error=str(exc))
            try:
                self._send_json(500, {"error": "exec failed"})
            except Exception:  # noqa: S110 - response failure on a closing socket; audited above
                pass
        finally:
            _semaphore.release()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> None:
    if not EXEC_TOKEN or len(EXEC_TOKEN) < 16:
        # Fail fast — refuse to start without a real shared secret.
        print(
            json.dumps({"error": "EXEC_TOKEN env var missing or too short (>= 16 chars)"}),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)

    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    httpd.daemon_threads = True
    _audit(
        "exec_server_started",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        max_concurrency=MAX_CONCURRENCY,
        allowed_bin=sorted(ALLOWED_BIN),
        # Don't log the token. Just confirm length so an operator can check
        # the secret was wired through.
        token_len=len(EXEC_TOKEN),
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    # Optional dev convenience: when EXEC_ALLOW_DEV_TOKEN=1 and EXEC_TOKEN is
    # unset, mint a one-shot random token and inject it into the environment
    # before main() reads its module-level snapshot. Production path always
    # has the Bicep-provisioned token.
    if not EXEC_TOKEN and os.environ.get("EXEC_ALLOW_DEV_TOKEN") == "1":
        dev_token = _secrets.token_urlsafe(32)
        os.environ["EXEC_TOKEN"] = dev_token
        # Re-bind the module-level constant so _check_token() sees the new value.
        EXEC_TOKEN = dev_token
        print(
            json.dumps({"warn": "generated dev EXEC_TOKEN", "token": dev_token}),
            file=sys.stderr,
            flush=True,
        )
    main()
