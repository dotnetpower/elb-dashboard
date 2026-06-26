---
title: Fast-fail kombu probes + bounded Celery result backend connect
description: Five places that probe or write to Redis used the OS default TCP connect timeout (75-120 s on Linux). When the broker port is *filtered* rather than refused (test runs without a Redis container, WSL2 mirrored networking, a stopped sidecar that left the LB rule in place), the readiness probe, BLAST pre-flight, submit gate, terminal-exec healthz, and Celery `update_state` all blocked past every caller deadline — locally `pytest -n auto` deadlocked and CI mirror runs needed `-n0`. Added a `fast_probe_connection()` helper for kombu probes (2 s connect cap), an env-tunable cap on the redis result backend, and a shorter timeout on the terminal-exec healthz client. Full pytest now passes clean with `-n auto` (4685 in 2:18).
tags:
  - operate
  - blast
---

# Fast-fail kombu probes + bounded Celery result backend connect

## Motivation

Investigating "pytest -n auto hangs on this dev machine" surfaced a real
production issue: five places in the code path probe or write to Redis
without bounding the per-attempt TCP socket connect timeout. The kombu
`ensure_connection(timeout=N)` argument only bounds the outer *retry
loop* — the inner `sock.connect()` inherits the OS default (75-120 s
on Linux). On a host where the broker port is *filtered* rather than
refused (WSL2 mirrored networking, the test environment with no Redis
container, a stopped sidecar that leaves the load-balancer rule in
place), every probe blocks past any caller deadline.

The same root cause was masked previously as "xdist worker SIGKILLed"
in CI — workers were not OOM-killed, they were killed by the per-test
60 s `pytest-timeout` thread firing on a hanging Redis socket. CI
already worked around it with `-n0` (commit 3092d75), and pytest.ini
was carrying a 300 s `--session-timeout` safety net. With this fix the
underlying hang is gone and the suite runs clean at `-n auto` in 2:18.

## Fixes shipped

### 1. `fast_probe_connection()` helper (3 call sites)

[api/celery_app.py](../../../api/celery_app.py) — new helper that returns
a kombu `Connection` with the redis transport's `socket_connect_timeout`
bolted on (default 2 s, env-tunable). Used only for **probes** (readiness,
pre-flight, submit gate); production workers/producers keep the long,
retrying connect that lets them ride out a broker restart.

Updated call sites:

* [api/routes/health.py::readiness](../../../api/routes/health.py) — Redis
  component of `/api/health/ready`.
* [api/routes/blast/preflight.py::blast_pre_flight](../../../api/routes/blast/preflight.py)
  — `broker` check in the BLAST submit pre-flight.
* [api/services/blast/submit_gates.py::_gate_broker](../../../api/services/blast/submit_gates.py)
  — `broker` gate in the admission control evaluator.

### 2. Bounded Celery result backend connect

[api/celery_app.py](../../../api/celery_app.py) — added
`result_backend_transport_options={"socket_connect_timeout": …}` plus the
top-level `redis_socket_connect_timeout` / `redis_socket_timeout` keys that
Celery's redis result backend reads directly. Defaults 5 s connect / 30 s
read, both env-tunable:

* `CELERY_RESULT_BACKEND_CONNECT_TIMEOUT` (default 5)
* `CELERY_REDIS_SOCKET_CONNECT_TIMEOUT` (default 5)
* `CELERY_REDIS_SOCKET_TIMEOUT` (default 30)

Without these, every `self.update_state(state="PROGRESS", …)` inside a
Celery task would block on the OS connect timeout when the result backend
is briefly unreachable, tarpiting each progress checkpoint and (in tests)
hanging the suite at the per-test timeout.

### 3. Shorter `terminal_exec.healthz()` timeout

[api/services/terminal_exec.py](../../../api/services/terminal_exec.py) —
the `/healthz` probe to the loopback exec server reused
`DEFAULT_HTTP_TIMEOUT = 10.0`, which was sized for `run()`-style writes,
not a single liveness GET. New `HEALTHZ_HTTP_TIMEOUT = 2.0` (env override
`TERMINAL_EXEC_HEALTHZ_TIMEOUT`) keeps repeated readiness probes from
amplifying into minutes when the sidecar is genuinely down.

## Validation

* `uv run ruff check api` → All checks passed.
* `uv run pytest api/tests` (default pytest.ini, `-n auto`) → **4685 passed,
  3 skipped in 2:18**. Same suite previously hung in `[gw3] node down: Not
  properly terminated` near 95 %.
* `uv run pytest api/tests -n0` (serial / CI mirror) → confirmed pass on
  the affected slice (`test_smoke.py` 85, `test_blast_submit_gates.py` 41,
  `test_response_contracts.py` 5, `test_openapi_rebuild.py` 11).

## Risk

Low. Each fix is a tighter timeout on a network call that previously
blocked indefinitely:

* `fast_probe_connection` only changes the **probe** call sites; the
  production worker still uses the default `celery_app.connection()` with
  the long retrying connect.
* The result backend timeouts apply to a fire-and-forget telemetry write
  inside a `try/except` already designed to swallow backend hiccups
  (`record_progress` in [api/tasks/openapi/helpers.py](../../../api/tasks/openapi/helpers.py)),
  so a transient connect failure now surfaces in 5 s instead of 75-120 s
  — strictly an improvement.
* All four caps ship default-OFF-equivalent env tunables, so ops can
  relax any of them without a redeploy if a deployment ever puts the
  worker on a high-latency link.

## Why this wasn't caught earlier

The dev-test environment relies on `redis.Redis.from_url` being wrapped
by an autouse fixture (`_redis_connect_fast_fail` in `api/tests/conftest.py`)
that injects `socket_connect_timeout=0.1`. That wrapper only catches the
two `redis_clients` constructors — kombu's redis transport and Celery's
result backend both build connections through paths that bypass
`from_url`. The hang therefore reproduced only on hosts where loopback
TCP to port 6379 is *filtered* (no Redis container running and the kernel
silently drops the SYN rather than RSTing it). CI was previously running
with a Redis sidecar that masked the filtered-port case, and the WSL2
networking mode introduced the filtered behaviour on the dev host.
