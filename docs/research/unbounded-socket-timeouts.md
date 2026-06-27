---
title: Unbounded Socket Timeouts — Audit & Lessons (2026-06-27)
description: Postmortem of the day-long sweep that traced "xdist worker SIGKILL" to five Redis-touching call sites inheriting the OS default TCP connect timeout. Documents the misdiagnosis history, the real root cause, the fix shape, the audit method, and the empirical false-positive ratio so future regressions are easier to catch.
tags:
  - research
  - operate
  - blast
---

# Unbounded Socket Timeouts — Audit & Lessons

Date: 2026-06-27

## Why this page exists

Across one session we shipped seven commits that look unrelated on the surface
but share a single underlying lesson: **a high-level `timeout=N` argument is
not the same as a bounded TCP connect**. Most network libraries (kombu, raw
`urllib`, the Azure SDK family) expose a retry-loop timeout and a per-attempt
socket-connect timeout as two different settings, and only the latter actually
bounds `sock.connect()`.

Without writing this down somewhere, the next reviewer who sees
`ensure_connection(timeout=2)` will think the call is already capped — and the
next "pytest hangs locally" will be misdiagnosed as OOM for another six months
(which is exactly what happened on this codebase between commits `08a6e85`
and `5a9cef6`).

## The misdiagnosis chain

| Commit | Diagnosis at the time | Action taken |
|---|---|---|
| `08a6e85` (2026-06-14) | "background ARM refresh blocked at interpreter shutdown" | `DaemonRefreshPool` + `--max-worker-restart=4` + `--session-timeout=300` |
| `3092d75` (2026-06-14) | "xdist worker SIGKILLed by OOM" | Forced CI to `pytest -n0` (serial) — accepted ~4 min CI cost as the price of stability |
| `5a9cef6` (2026-06-27) | **Real**: kombu / Celery result-backend / terminal-exec `healthz` calls inherited the OS default 75-120 s TCP connect timeout on a *filtered* port; `pytest-timeout=60` then SIGKILLed the still-running worker, which xdist reported as "node down" — which we read as OOM | Centralised `fast_probe_connection()` helper, env-tunable `redis_socket_*timeout`, shorter `terminal_exec.HEALTHZ_HTTP_TIMEOUT` |
| `a423adf` (2026-06-27) | Underlying hang resolved | Restored CI to `pytest` (no `-n0`) — full suite back to ~2 min on 4-vCPU runners |

The OOM hypothesis was wrong in two reinforcing ways:

* Workers were not memory-killed. They were `pytest-timeout`-killed because a
  C-level `sock.connect()` couldn't be interrupted by the per-test 60 s
  thread-method timeout, so when the alarm fired pytest had to kill the worker
  process. The trace stripping looked identical to OOM (no traceback, no
  faulthandler dump).
* "Filtered" is the key word. CI runners ran a real Redis sidecar so the local
  loopback connect was *refused* (RST → instant `ConnectionRefusedError`). The
  bug only reproduced on the WSL2 dev host whose mirrored networking *drops*
  the SYN, and on production hosts where a stopped sidecar leaves the LB rule
  in place. The mismatch between local-passes-CI-fails (and vice-versa) bought
  six months of confusion.

## What `timeout=` actually means per library

| Library | The `timeout` arg in `foo(timeout=N)` | What bounds `sock.connect()` |
|---|---|---|
| `kombu.Connection.ensure_connection(timeout=N)` | Total retry-loop budget. Does **not** bound the inner `transport.establish_connection()` call. | `transport_options={"socket_connect_timeout": N}` |
| Celery result backend (redis) | n/a — defaults to OS connect timeout | `redis_socket_connect_timeout` / `result_backend_transport_options={"socket_connect_timeout": ...}` |
| `redis.Redis(...)` direct | `socket_timeout` = read/write timeout. Does **not** bound connect. | `socket_connect_timeout` |
| `requests.Session.get(timeout=N)` | When scalar, applies to both connect and read. ✓ bounded | Same |
| `urllib.request.urlopen(req, timeout=N)` | ✓ bounded — but the kwarg is **omitted** by default (= `None` = no timeout) | Same |
| `httpx.Client(timeout=...)` | ✓ bounded by default (5 s connect / 5 s read / 5 s write / 5 s pool) | Same — but `timeout=None` opts out |
| `azure-core` (BlobServiceClient, etc.) | `connection_timeout` (default 300 s) + `read_timeout` (default 300 s) | Same — bounded but generous (5 min per attempt × `retry_total` retries) |
| `azure-servicebus` | `retry_total` (default 3) + `retry_backoff_max` (default 120 s) | Internal AMQP transport — no public `socket_connect_timeout`; the retry caps are the only knob |
| `socket.socket().connect()` | Set via `s.settimeout(N)` **before** `connect()` | Same — only path |
| `subprocess.run(...)` | Pass `timeout=N` explicitly — default is `None` = block forever | Same |

> Anywhere the second column says "**Does not bound `sock.connect()`**", a
> filtered destination port hangs for the OS default.

## The fix shape we converged on

Three layers of defence, all env-tunable so an operator can relax without a
redeploy:

1. **Bound the per-attempt TCP connect.** Either via the library's dedicated
   knob (`socket_connect_timeout`, `transport_options={"socket_connect_timeout": ...}`,
   `redis_socket_connect_timeout`) or by setting it on the socket before
   `connect()` (`api/wait_redis.py`).
2. **Bound the retry loop.** Where the library has internal retry
   (`azure-servicebus` `retry_total` / `retry_backoff_max`, kombu
   `max_retries`/`timeout`), set explicit caps. Default 3 retries × 120 s
   backoff = ~6 min is too generous for dashboard-triggered probes.
3. **Bound the operation wall-clock.** Per-test `pytest-timeout=60`,
   per-request server timeout, per-Celery task `task_time_limit`. These catch
   the case where layers 1 and 2 are skipped or misconfigured.

## Sites we capped this session

| Site | Layer | Default after fix | Env override |
|---|---|---|---|
| `api/celery_app.py::fast_probe_connection` (3 probe call sites) | TCP connect | 2 s | none — change the constant |
| `api/celery_app.py::celery_app.conf` (result backend) | TCP connect | 5 s | `CELERY_RESULT_BACKEND_CONNECT_TIMEOUT`, `CELERY_REDIS_SOCKET_CONNECT_TIMEOUT`, `CELERY_REDIS_SOCKET_TIMEOUT` |
| `api/services/terminal_exec.py::healthz` | HTTP connect | 2 s | `TERMINAL_EXEC_HEALTHZ_TIMEOUT` |
| `api/services/service_bus.py::_client / _admin_client` | retry total + backoff | `retry_total=3`, `retry_backoff_max=30` s | `SERVICEBUS_RETRY_TOTAL`, `SERVICEBUS_RETRY_BACKOFF_MAX` |
| `api/services/service_bus_external_consumer.py::_client` | same | inherits via `_sb_client_kwargs()` | same |
| `scripts/dev/render_release_notes.py::git` | subprocess | 30 s | none — change the constant |
| `api/services/blast/workflow_export.py::_submit_script` (generated `urlopen`) | HTTP connect | 60 s | `ELB_SUBMIT_TIMEOUT` (set in the exported workflow) |

## Audit method that worked

The subagent-driven "find all unbounded timeouts" sweep had a roughly
**1-in-3 true-positive rate** (4 real findings out of ~14 candidates). The
discriminator is always *read the source, do not trust the grep*:

* `requests.get` matches in `k8s/ingress.py` and `k8s/node_pressure.py` were
  dict `.get()` calls on a local `requests` variable, not the HTTP library.
* `_eutils.py` `while True:` *did* check a `deadline` ten lines below the
  loop header — the agent stopped reading too early.
* SSE heartbeat constants flagged as "Medium" risk were all `15-25 s`, well
  inside the Container Apps 240 s idle timeout — sane by design.
* Azure SDK clients flagged as "Critical / OS default" were actually
  `retry_total=3 × 300 s` bounded — bad, but not infinite. Worth tightening,
  not panicking.

False positives are cheap to clear (one `read_file` per finding); the
expensive failure mode is missing a real one. The 1-in-3 ratio is fine.

## Discriminators worth grepping for the next time

```bash
# kombu / celery direct callers — easy to spot, fewer than five in this repo
rg -n "kombu\.Connection\(|celery_app\.connection\(\)|app\.connection\(\)" api/

# urllib without timeout — note urlopen's timeout kwarg is optional
rg -nP "urllib\.request\.urlopen\b" api/ scripts/ | rg -v "timeout\s*="

# subprocess without timeout — N.B. all api/tests/* are exempt (test code
# can block; we want to catch SUT bugs)
rg -nP "subprocess\.(run|check_output|check_call|Popen)\b" api/ scripts/ \
  | rg -v "timeout\s*=" | rg -v "^api/tests/"

# socket.connect — settimeout must come BEFORE connect
rg -n "\.connect\(\s*\(" api/

# Generated user-facing code emitted from string templates — easy to miss
# because the offending line is inside a Python string literal
rg -n "urllib\.request\.urlopen" api/services/blast/workflow_export.py
```

## The "ship safety net, not a fix" question

Twice this session we considered extending
`conftest._redis_connect_fast_fail` to also wrap kombu / celery. We did not,
because:

* The production fix (`fast_probe_connection`, `redis_socket_connect_timeout`,
  bounded `retry_*`) already prevents the hang.
* A test-side wrapper would *hide* a future caller that bypasses the helper,
  letting the same class of regression land silently.

Per-test fast-fail wrappers are appropriate when the library has no
production knob (e.g. wrapping `redis.Redis.from_url` because there is no
deployment-wide way to inject `socket_connect_timeout`). When the library
*has* the knob, set it in production code so the test surface and the prod
surface stay aligned.

## Footnotes

* The kernel OOM analysis in commit `3092d75` was published in good faith
  based on the symptom set (no traceback, no faulthandler dump, worker
  process killed). Reading that commit message after the fact will
  legitimately make a future engineer reach for "raise memory" — the lesson
  is that "no traceback" is also the signature of *any* subprocess SIGKILL,
  including the one `pytest-timeout` performs when its thread-method alarm
  cannot interrupt a C-level syscall.
* `--session-timeout=300` in `pytest.ini` is now load-bearing: it is the
  controller-side wall-clock cap that aborts the suite with
  `xdist.dsession.Interrupted` instead of hanging if a future regression
  re-introduces a similar blocking call. Do not remove it.
