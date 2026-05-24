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
ALLOWED_BIN: frozenset[str] = frozenset(
    {"azcopy", "kubectl", "elastic-blast", "elb", "az", "git"}
)
LISTEN_HOST = os.environ.get("EXEC_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("EXEC_PORT", "7682"))
EXEC_TOKEN = os.environ.get("EXEC_TOKEN", "")
MAX_CONCURRENCY = int(os.environ.get("EXEC_MAX_CONCURRENCY", "4"))
MAX_BODY_BYTES = int(os.environ.get("EXEC_MAX_BODY_BYTES", str(64 * 1024)))
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 1800  # hard 30-min cap; longer tasks must be split
SIGTERM_GRACE_SECONDS = 5
EXEC_TMP_ROOT = "/tmp/exec"  # noqa: S108 - intentional; cleaned per-request
# Per-NDJSON-line cap for the streaming ``_stream`` response. A child can
# emit single lines of hundreds of MB (binary blob accidentally dumped to
# stdout, az --debug spew with no embedded newlines, …); without a cap
# ``pipe.readline()`` would keep growing one bytearray until OOM. Lines
# longer than the cap are sent with the captured prefix + a marker, then
# the rest of that line is drained so the next read starts cleanly.
NDJSON_LINE_MAX_BYTES = int(os.environ.get("EXEC_STREAM_LINE_MAX_BYTES", str(64 * 1024)))
# Periodic GC for stale request workdirs. The per-request ``finally``
# already cleans up on the happy path; this catches the cases where the
# server was SIGKILL'd mid-request (Container Apps revision rollover,
# OOMKill) or where the SIGTERM cleanup raced with the child holding an
# open fd into the dir.
EXEC_TMPDIR_GC_INTERVAL_SECONDS = int(
    os.environ.get("EXEC_TMPDIR_GC_INTERVAL_SECONDS", "300")
)
EXEC_TMPDIR_GC_MAX_AGE_SECONDS = int(
    os.environ.get("EXEC_TMPDIR_GC_MAX_AGE_SECONDS", "3600")
)


def _run_output_max_bytes() -> int:
    """Resolve the run() output cap at request time.

    Read from env on every call so tests (and ops) can rotate the cap
    without redeploying the sidecar. Production sets the env once at
    container startup so the lookup is effectively free.
    """
    try:
        return int(os.environ.get("EXEC_RUN_MAX_OUTPUT_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        return 8 * 1024 * 1024


# Module-level constant kept for backwards compatibility / introspection. The
# live value flows through ``_run_output_max_bytes()`` so a test or operator
# can mutate ``EXEC_RUN_MAX_OUTPUT_BYTES`` after import.
RUN_OUTPUT_MAX_BYTES = _run_output_max_bytes()
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
    # Force unbuffered stdout/stderr in Python children so the streaming NDJSON
    # response sees each printed line in real time. Without this, elastic-blast
    # (a Python CLI) block-buffers stdout into 8 KB chunks when its stdout is a
    # pipe, which surfaces in the dashboard as long stalls followed by a burst.
    env.setdefault("PYTHONUNBUFFERED", "1")
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


def _drain_capped(pipe, cap: int) -> tuple[bytes, bool]:
    """Read ``pipe`` until EOF, return (bytes_buffered, truncated)."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            if total >= cap:
                # Keep draining so the writer (child) doesn't block on a
                # full pipe, but discard the data so we stay under cap.
                truncated = True
                continue
            remaining = cap - total
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total = cap
                truncated = True
            else:
                chunks.append(chunk)
                total += len(chunk)
    except (BrokenPipeError, OSError):
        pass
    return b"".join(chunks), truncated


def _run_buffered(req: dict) -> dict:
    """Run the command, capture bounded output, return as a single dict.

    Replaces the prior ``proc.communicate(...)`` path so a verbose child
    (``elastic-blast submit`` log output, ``az`` debug output) cannot OOM
    the terminal sidecar. Output above ``RUN_OUTPUT_MAX_BYTES`` is dropped
    on the floor and the response carries ``stdout_truncated`` / ``stderr_truncated``
    so the caller can degrade gracefully.
    """
    cwd, owned = _make_cwd(req["cwd"])
    started = time.monotonic()
    out_holder: list[bytes] = []
    err_holder: list[bytes] = []
    truncated_flags = {"stdout": False, "stderr": False}
    cap = _run_output_max_bytes()

    def _reader(pipe, key: str, dest: list[bytes]) -> None:
        data, truncated = _drain_capped(pipe, cap)
        dest.append(data)
        truncated_flags[key] = truncated

    try:
        _write_stdin_file(req, cwd)
        proc = _spawn(req["argv"], cwd, req["stdin"])
        # Send stdin (if any) without holding the whole output in RAM —
        # ``communicate`` would do that for us, but here we drain via
        # capped reader threads.
        stdin_bytes = req["stdin"].encode("utf-8") if req["stdin"] is not None else None
        if stdin_bytes is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_bytes)
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.stdin.close()
            except Exception:  # noqa: S110 - stdin close races with subprocess exit
                pass
        stdout_thread = threading.Thread(
            target=_reader, args=(proc.stdout, "stdout", out_holder), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_reader, args=(proc.stderr, "stderr", err_holder), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            proc.wait(timeout=req["timeout"])
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_proc(proc)
            try:
                proc.wait(timeout=SIGTERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            timed_out = True
        # Make sure the reader threads have drained before we return.
        stdout_thread.join(timeout=SIGTERM_GRACE_SECONDS)
        stderr_thread.join(timeout=SIGTERM_GRACE_SECONDS)
        out = b"".join(out_holder)
        err = b"".join(err_holder)
        return {
            "exit_code": proc.returncode if not timed_out else 124,
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": err.decode("utf-8", errors="replace"),
            "stdout_truncated": truncated_flags["stdout"],
            "stderr_truncated": truncated_flags["stderr"],
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
        cap = NDJSON_LINE_MAX_BYTES
        while True:
            try:
                # readline(size) returns at most ``size`` bytes OR up to and
                # including the next \n, whichever comes first. We ask for
                # one byte past the cap so we can detect over-cap lines
                # (size==cap+1 with no trailing \n).
                raw = pipe.readline(cap + 1)
            except (BrokenPipeError, OSError):
                break
            if not raw:
                break
            truncated = False
            if len(raw) > cap and not raw.endswith(b"\n"):
                # Drain the rest of this oversized line so the next
                # ``readline`` starts cleanly. Bounded by the child's
                # output cadence; we use 8 KiB reads so the drain is fast.
                truncated = True
                while True:
                    try:
                        chunk = pipe.readline(8192)
                    except (BrokenPipeError, OSError):
                        break
                    if not chunk or chunk.endswith(b"\n"):
                        break
            try:
                decoded = raw.decode("utf-8", errors="replace").rstrip("\n")
                if truncated:
                    decoded += " [truncated:line-over-cap]"
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
def _gc_stale_tmpdirs(max_age_seconds: int) -> int:
    """Remove ``EXEC_TMP_ROOT/req-*`` dirs older than ``max_age_seconds``.

    Returns the number of dirs removed. Cheap: ``os.scandir`` + ``stat`` per
    entry. Safe to call concurrently with ``_make_cwd`` because we only
    touch dirs whose mtime is older than the cap; an in-flight request is
    actively mutating its workdir so its mtime is fresh.
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    if not os.path.isdir(EXEC_TMP_ROOT):
        return 0
    try:
        entries = list(os.scandir(EXEC_TMP_ROOT))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith("req-"):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff:
            continue
        try:
            shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue
        removed += 1
    return removed


def _start_tmpdir_gc_thread() -> None:
    """Spawn a daemon thread that periodically prunes stale request workdirs."""
    if EXEC_TMPDIR_GC_INTERVAL_SECONDS <= 0:
        return

    def _loop() -> None:
        # Initial sweep catches anything left behind by the previous
        # container instance before the first request even arrives.
        try:
            removed = _gc_stale_tmpdirs(0)
            if removed:
                _audit("exec_tmpdir_initial_gc", removed=removed)
        except Exception as exc:  # pragma: no cover - defensive
            _audit("exec_tmpdir_initial_gc_failed", error=type(exc).__name__)
        while True:
            time.sleep(EXEC_TMPDIR_GC_INTERVAL_SECONDS)
            try:
                removed = _gc_stale_tmpdirs(EXEC_TMPDIR_GC_MAX_AGE_SECONDS)
                if removed:
                    _audit(
                        "exec_tmpdir_gc",
                        removed=removed,
                        max_age=EXEC_TMPDIR_GC_MAX_AGE_SECONDS,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                _audit("exec_tmpdir_gc_failed", error=type(exc).__name__)

    threading.Thread(target=_loop, daemon=True, name="exec-tmpdir-gc").start()


def main() -> None:
    if not EXEC_TOKEN or len(EXEC_TOKEN) < 16:
        # Fail fast — refuse to start without a real shared secret.
        print(
            json.dumps({"error": "EXEC_TOKEN env var missing or too short (>= 16 chars)"}),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)
    if len(EXEC_TOKEN) > 1024:
        print(
            json.dumps({"error": "EXEC_TOKEN env var too long (<= 1024 chars)"}),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)

    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    httpd.daemon_threads = True
    _start_tmpdir_gc_thread()
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
