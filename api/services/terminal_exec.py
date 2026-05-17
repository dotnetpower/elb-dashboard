"""terminal_exec — programmatic shell channel into the `terminal` sidecar.

WHEN to reach for this module
-----------------------------
You almost never should. The vast majority of work the control plane does is
already covered by:

  * `azure.mgmt.*` Python SDKs (loaded directly in the api/worker images via
    `api.services.azure_clients`) — for ARM operations.
  * `kubernetes` Python client wrapped by `api.services.monitoring.k8s_*`
    helpers — for Kubernetes operations (uses the kubeconfig token directly,
    no `kubectl` process needed).
  * `azure.storage.blob` SDK in `api.services.storage_data` — for blob
    upload/list/read.
  * `azure.data.tables` and `azure.storage.blob` for state — see
    `api.services.state_repo`.

`terminal_exec` is for the genuinely-shell-only cases:

  * `azcopy` for very large parallel transfers that the SDK cannot match.
  * `elastic-blast` CLI invocations that the operator currently runs by hand
    in the browser terminal but that we want to script from a Celery task.
  * `kubectl exec` into BLAST pods.

Why NOT Run Command
-------------------
* `ManagedClusters.begin_run_command` and `VirtualMachines.begin_run_command`
  both take ~30 s for trivial operations (poll-based async ARM endpoint).
* They are managed by the resource provider, not by us — failures are opaque
  and rate-limited.
* They require ARM RBAC writes and audit-log every invocation, which is
  costly for high-frequency monitoring polls.

The terminal sidecar already ships every CLI we need (`azure-cli`, `kubectl`,
`azcopy`, `elastic-blast`, `python3.12`, `tmux`, `jq`, `git`, `make`,
`primer3`, `ttyd`). It runs in the same Container App revision as the api /
worker / beat sidecars, so loopback (`127.0.0.1`) RPC has zero network cost.

Wire model
----------

  api / worker sidecar                       terminal sidecar
  ┌──────────────────┐                     ┌────────────────────────────┐
  │ terminal_exec    │  POST /exec[/stream]│  exec_server.py            │
  │ .run() / .stream()├────────────────────►│  (stdlib http.server)      │
  │                  │ 127.0.0.1:7682      │                            │
  │ X-Exec-Token: …  │  shared secret  ────►  hmac.compare_digest        │
  │                  │                     │  argv[0] allowlist         │
  │                  │                     │  Semaphore(EXEC_MAX_…)     │
  │                  │                     │  /tmp/exec/<uuid> cwd      │
  │                  │  JSON / NDJSON ◄────┤  subprocess.Popen          │
  └──────────────────┘                     └────────────────────────────┘

The shared secret is provisioned by Bicep as a Container Apps secret
(`exec-token`) and injected into both sidecars via `secretRef`. It rotates
on every deployment.

Concurrency: the exec server caps in-flight requests at
``EXEC_MAX_CONCURRENCY`` (default 4). Excess requests are rejected with HTTP
503 — Celery's task retry policy is the right place to back off.

Output sanitisation: stdout/stderr come back raw from the subprocess.
``run()`` runs them through ``api.services.sanitise.sanitise`` before
returning. ``stream()`` yields raw lines (so progress parsers like
``azcopy --output-type=json`` get the bytes the tool actually produced); the
caller is responsible for sanitising before forwarding to any HTTP /
WebSocket boundary.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import httpx

from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

EXEC_UPSTREAM_ENV = "TERMINAL_EXEC_UPSTREAM"
EXEC_TOKEN_ENV = "EXEC_TOKEN"  # noqa: S105 - env var NAME, not a value
DEFAULT_UPSTREAM = "http://127.0.0.1:7682"
DEFAULT_HTTP_TIMEOUT = 10.0  # connect / write timeout to the exec server itself

# Hard ceiling for execution time. The exec server enforces its own cap; this
# is just the request-side timeout so the api sidecar doesn't dangle forever
# if the exec server hangs. Add 30 s slack so the server has time to send the
# summary line for a job that ran the full timeout.
_HTTP_READ_SLACK_SECONDS = 30.0


class TerminalExecError(RuntimeError):
    """Raised when the exec server is unreachable, mis-authenticated, or
    refuses the request (allowlist / size / concurrency).

    A subprocess that exits non-zero is NOT an error here — it surfaces as a
    non-zero ``exit_code`` in the returned dict.
    """


def _upstream() -> str:
    return os.environ.get(EXEC_UPSTREAM_ENV, DEFAULT_UPSTREAM).rstrip("/")


def _token() -> str:
    token = os.environ.get(EXEC_TOKEN_ENV, "")
    if not token:
        raise TerminalExecError(
            f"{EXEC_TOKEN_ENV} env var is empty; the api sidecar cannot "
            "authenticate with the terminal exec server. Check Bicep "
            "containerAppControl.bicep `exec-token` secret + sidecar env."
        )
    return token


def _headers() -> dict[str, str]:
    return {"X-Exec-Token": _token(), "Content-Type": "application/json"}


def _payload(
    argv: list[str],
    *,
    stdin: str | None,
    stdin_file: str | None,
    cwd: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not argv:
        raise ValueError("argv must be a non-empty list")
    return {
        "argv": list(argv),
        "stdin": stdin,
        "stdin_file": stdin_file,
        "cwd": cwd,
        "timeout_seconds": int(timeout_seconds),
    }


def _http_timeout(timeout_seconds: int) -> httpx.Timeout:
    """Timeout for buffered ``run()``: server has up to ``timeout_seconds``
    to finish; client gives the server an extra 30 s to flush the response."""
    return httpx.Timeout(
        connect=DEFAULT_HTTP_TIMEOUT,
        read=timeout_seconds + _HTTP_READ_SLACK_SECONDS,
        write=DEFAULT_HTTP_TIMEOUT,
        pool=DEFAULT_HTTP_TIMEOUT,
    )


def _stream_http_timeout() -> httpx.Timeout:
    """Timeout for streaming ``stream()``: ``read=None`` so we wait
    indefinitely between NDJSON lines (long-running tools like ``azcopy``
    can have minutes of silence between progress lines). The server-side
    ``timeout_seconds`` is the only ceiling; we still cap connect / write
    so a stuck TCP handshake fails fast."""
    return httpx.Timeout(
        connect=DEFAULT_HTTP_TIMEOUT,
        read=None,
        write=DEFAULT_HTTP_TIMEOUT,
        pool=DEFAULT_HTTP_TIMEOUT,
    )


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    stdin_file: str | None = None,
    cwd: str | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Execute a single command in the terminal sidecar, return final result.

    Parameters
    ----------
    argv : list[str]
        Command + args. ``argv[0]`` MUST be in the exec server's allowlist
        (currently ``{azcopy, kubectl, elastic-blast, elb, az}``).
    stdin : str | None
        Data piped to the subprocess's stdin. ``None`` means closed stdin.
    stdin_file : str | None
        Relative file path to write ``stdin`` into inside the execution cwd
        before starting the subprocess. Useful for CLIs that require a config
        file path and do not accept ``-`` as stdin.
    cwd : str | None
        Absolute path to run in. ``None`` means a fresh ``/tmp/exec/<uuid>``
        directory that the exec server will clean up.
    timeout_seconds : int
        Per-execution timeout. Capped server-side at 1800 s.

    Returns
    -------
    dict
        ``{"exit_code": int, "stdout": str, "stderr": str,
           "duration_ms": int, "timed_out": bool}``. ``stdout``/``stderr``
        are pre-sanitised (SAS / bearer / sub-id redacted).

    Raises
    ------
    TerminalExecError
        When the exec server is unreachable, returns a non-2xx status, or
        rejects the request (bad allowlist / body size / concurrency).
    """
    body = _payload(
        argv,
        stdin=stdin,
        stdin_file=stdin_file,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    try:
        with httpx.Client(timeout=_http_timeout(timeout_seconds)) as client:
            resp = client.post(_upstream() + "/exec", headers=_headers(), json=body)
    except httpx.HTTPError as exc:
        raise TerminalExecError(f"exec server unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise TerminalExecError(
            f"exec server returned {resp.status_code}: {sanitise(resp.text)[:300]}"
        )

    result = resp.json()
    if "stdout" in result:
        result["stdout"] = sanitise(result["stdout"])
    if "stderr" in result:
        result["stderr"] = sanitise(result["stderr"])
    return result


def stream(
    argv: list[str],
    *,
    stdin: str | None = None,
    stdin_file: str | None = None,
    cwd: str | None = None,
    timeout_seconds: int = 300,
) -> Iterator[dict[str, Any]]:
    """Execute a long-running command; yield one dict per output line.

    Each yielded dict is one of::

        {"stream": "stdout", "line": "<text>"}
        {"stream": "stderr", "line": "<text>"}
        {"exit_code": int, "duration_ms": int, "timed_out": bool}   # last item

    The summary line is always emitted last. Lines are yielded as they
    arrive so callers can render progress (e.g. azcopy's per-file lines)
    without waiting for completion.

    The lines are NOT sanitised — the caller is responsible for running
    each ``line`` field through ``api.services.sanitise.sanitise`` before
    forwarding to any HTTP / WebSocket boundary.
    """
    body = _payload(
        argv,
        stdin=stdin,
        stdin_file=stdin_file,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    try:
        with httpx.Client(timeout=_stream_http_timeout()) as client:
            with client.stream(
                "POST", _upstream() + "/exec/stream", headers=_headers(), json=body
            ) as resp:
                if resp.status_code != 200:
                    raw = resp.read().decode("utf-8", errors="replace")
                    raise TerminalExecError(
                        f"exec server returned {resp.status_code}: {sanitise(raw)[:300]}"
                    )
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        yield json.loads(raw_line)
                    except json.JSONDecodeError:
                        # Skip malformed lines but keep the stream open; the
                        # exec server's last line is always valid JSON, so
                        # the caller still gets the summary.
                        LOGGER.warning("exec stream produced non-JSON line; skipping")
                        continue
    except httpx.HTTPError as exc:
        raise TerminalExecError(f"exec server unreachable: {exc}") from exc


def healthz() -> dict[str, Any]:
    """Probe the exec server's /healthz (no auth required). Returns the JSON
    body; raises ``TerminalExecError`` if the server is unreachable or
    returns non-200."""
    try:
        with httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT) as client:
            resp = client.get(_upstream() + "/healthz")
    except httpx.HTTPError as exc:
        raise TerminalExecError(f"exec server unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise TerminalExecError(
            f"exec server /healthz returned {resp.status_code}: {sanitise(resp.text)[:200]}"
        )
    return resp.json()
