# 2026-05-15 — Programmatic exec channel into the terminal sidecar; retire Run Command

**Scope**: `terminal/exec_server.py` (new), `terminal/entrypoint.sh`,
`terminal/Dockerfile`, `api/services/terminal_exec.py` (new),
`api/services/monitoring.py`, `api/services/storage_data.py`,
`api/tests/test_terminal_exec.py` (new),
`infra/modules/containerAppControl.bicep`.

## Motivation

Two pain points:

1. **Azure Run Command** (`ManagedClusters.begin_run_command`,
   `VirtualMachines.begin_run_command`) is ~30 s slow, ARM-rate-limited,
   and audit-logs every invocation. Even though no active code called it
   anymore (`monitoring.run_aks_command` had 0 callers; `compute._run_command`
   only existed for the retired Remote Terminal VM), the helpers were a
   future-regression footgun: an implementer reaching for a "30 s `kubectl
   get pods` slow path" instead of `monitoring.k8s_get_pods` (tens of ms).

2. The `terminal` sidecar already ships every CLI we need (`azcopy`,
   `kubectl`, `elastic-blast`, `az`, …) but the only browser→sidecar path
   was the interactive `ttyd` WebSocket. Celery tasks had no programmatic
   way to call shell tooling without either:
   (a) baking the toolchain into the api/worker images (bloat + duplication),
   or (b) reaching for Run Command (slow + footgun).

## User-facing change

None today — no Celery task wires `terminal_exec` yet. The contract +
runtime + tests + Bicep wiring all land so the next phase-2 task PR
(BLAST submit / azcopy upload / etc.) is a small change instead of a
multi-day mechanism build.

## API / IaC diff summary

### `terminal/exec_server.py` (new, ~430 lines, stdlib only)

Loopback HTTP server on `127.0.0.1:7682`:

- **Auth**: `X-Exec-Token` header, compared with `hmac.compare_digest`
  against the `EXEC_TOKEN` env var (Container Apps secret reference, see
  Bicep below).
- **Allowlist**: hardcoded
  `ALLOWED_BIN = {azcopy, kubectl, elastic-blast, elb, az}`. Any other
  `argv[0]`, or one containing `/`, `\`, or starting with `.`, → 403.
- **Concurrency cap**: `BoundedSemaphore(EXEC_MAX_CONCURRENCY=4)`. Excess
  → 503 immediately (no blocking; Celery's retry policy is the right
  back-off layer).
- **Per-request cwd**: `tempfile.mkdtemp(dir="/tmp/exec/", …)`, cleaned in
  `finally`. Explicit absolute `cwd` honored as-is.
- **Subprocess**: `subprocess.Popen(argv, shell=False,
  start_new_session=True)`; on timeout `os.killpg(SIGTERM)` + 5 s grace +
  `SIGKILL`. Returns `exit_code=124, timed_out=True` on hit.
- **Body cap**: `MAX_BODY_BYTES=64 KB`.
- **Slowloris defense**: `_Handler.timeout = 15` (per-request socket
  timeout for header + body). After body parse,
  `_relax_socket_for_long_response()` extends to `timeout_seconds + slack`
  so streaming responses are not killed mid-flight.
- **Streaming `/exec/stream`**: NDJSON (`Content-Type:
  application/x-ndjson`), one line per stdout/stderr line, summary line
  last (`{exit_code, duration_ms, timed_out, client_aborted}`).
- **Client-disconnect kill**: `_write_line` catches
  `BrokenPipeError`/`ConnectionResetError` → flips `client_alive` flag →
  `_stream`'s polling loop calls `_kill_proc(proc)` early instead of
  letting the subprocess run for the full timeout.
- **Audit log**: one JSON line to stderr per request
  (`exec_start` / `exec_done` / `exec_error`) — bin name only, never
  argv[1:] / stdin / stdout / token.
- **Boot**: refuses to start if `EXEC_TOKEN` empty or `< 16 chars`.

### `terminal/entrypoint.sh` — supervisor pattern (rewritten)

Was: `python3 exec_server &; exec ttyd …` + a background watchdog that
called `kill -TERM 1` if `EXEC_PID` died.

Problem: after `exec ttyd`, the watchdog gets reparented to the new
PID 1 (ttyd). When ttyd dies the watchdog's `kill -TERM 1` then targets a
non-existent PID and the container hangs as a zombie. Plus PID 1 ignores
`SIGTERM` by default unless a trap is registered.

Now: bash stays PID 1, ttyd + exec_server run as background children,
`trap 'shutdown TERM/INT/HUP' …` forwards signals to both, `wait -n`
returns the first child's RC, then 5 s grace + SIGKILL of the other
child + `exit "$FIRST_RC"`. Container Apps observes the non-zero exit
and restarts the revision.

Also: `python3` → `/usr/bin/python3.12` (PATH-immune).

### `terminal/Dockerfile`

- `EXPOSE 7681 7682`.
- `COPY exec_server.py /usr/local/bin/elb-exec-server` + `chmod +x`.

### `api/services/terminal_exec.py` (new)

Client side. Public API:

```python
def run(argv, *, stdin=None, cwd=None, timeout_seconds=60) -> dict
def stream(argv, *, stdin=None, cwd=None, timeout_seconds=300) -> Iterator[dict]
def healthz() -> dict
```

- Env: `TERMINAL_EXEC_UPSTREAM` (default `http://127.0.0.1:7682`),
  `EXEC_TOKEN`. Empty token → `TerminalExecError` with explicit reason.
- Custom `TerminalExecError(RuntimeError)` for transport / auth /
  allowlist / 503 failures. Non-zero subprocess `exit_code` is NOT an
  exception — it surfaces in the result dict.
- `run()` runs returned `stdout`/`stderr` through
  `api.services.sanitise.sanitise` (SAS / bearer / sub-id redacted).
  `stream()` yields raw lines (so progress parsers like
  `azcopy --output-type=json` get the bytes the tool produced); the route
  handler sanitises before forwarding to the HTTP boundary.
- `stream()` uses `httpx.Timeout(read=None)` (separate
  `_stream_http_timeout()` factory) so long-silent tools (azcopy, 5+ min
  between progress lines) don't trip a client idle timeout. Server-side
  `timeout_seconds` is the only ceiling.

### `api/services/monitoring.py`

- Removed `run_aks_command()` (~30 lines + 6-import) — was 0 callers.
- Replaced with a 23-line load-bearing comment naming the existing fast
  `k8s_*` surface (`k8s_get_nodes`, `k8s_get_pods`, `k8s_top_nodes`,
  `k8s_pod_logs`, `k8s_check_blast_status`, `k8s_warmup_status`,
  `k8s_get_service_ip`, `k8s_check_namespace_exists`) and forbidding
  re-introduction of `begin_run_command`. Future implementers must add a
  new `k8s_*` helper using `_get_k8s_session()` instead.

### `api/services/storage_data.py`

- Removed `generate_download_url()` + `generate_blob_sas` /
  `BlobSasPermissions` / `get_user_delegation_key` imports.
- Added a load-bearing comment forbidding re-introduction; routes today
  return 503 `streaming_proxy_pending`. The browser-streaming proxy will
  add a `stream_blob_to_response()` helper here in a future PR — never
  resurrect the SAS issuer.

### `infra/modules/containerAppControl.bicep`

- `@secure() param execToken string = newGuid()` — Bicep-managed shared
  secret, rotates on every deployment.
- `secrets: [{name: 'exec-token', value: execToken}]` on the Container
  App.
- `EXEC_TOKEN` env injected via `secretRef` into all three sidecars that
  participate (api, worker, terminal). `TERMINAL_EXEC_UPSTREAM=http://127.0.0.1:7682`
  added to api + worker.
- Terminal sidecar gains liveness + readiness probes hitting
  `:7682/healthz` (no auth required) so a hung HTTP server with a live
  PID still triggers a Container Apps restart (second line of defense
  behind the entrypoint supervisor).

## Validation

- `uv run pytest -q api/tests/test_terminal_exec.py` → 11 passed end-to-end
  (token auth pos+neg, allowlist, run() + sanitisation + non-zero exit +
  timeout=124, stream() ordering + summary, concurrency cap → 503).
- `uv run pytest -q api/tests` → **56 passed** (45 baseline + 11 new).
- `uv run ruff check terminal/exec_server.py api/services/terminal_exec.py
  api/tests/test_terminal_exec.py` → all checks passed.
- `az bicep build infra/main.bicep` exit 0; compiled ARM verified to wire
  `EXEC_TOKEN secretRef` into api + worker + terminal sidecars.
- `bash -n terminal/entrypoint.sh` clean.

## Cross-repo consistency

`.github/copilot-instructions.md` §11 + `AGENTS.md` tripwire #9 cite the
ban on Run Command and point at `api/services/terminal_exec.py` as the
sole shell-only path.
