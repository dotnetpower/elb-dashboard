---
title: Retry dropped AKS keepalive sockets on k8s GETs
description: Fix the noisiest App Insights exception by retrying RemoteDisconnected read aborts on idempotent Kubernetes GET requests.
tags:
  - operate
  - architecture
---

# Retry dropped AKS keepalive sockets on k8s GETs (#45)

## Motivation

`requests.exceptions.ConnectionError` was the single highest-volume App Insights
exception (85 events / 7 days, ongoing). The AKS API server silently drops idle
pooled keepalive sockets; the next GET on that dead socket raised:

```
ConnectionError(ProtocolError('Connection aborted.',
RemoteDisconnected('Remote end closed connection without response')))
```

The warmup pod-log fan-out (`api/services/k8s/warmup_status.py`) already caught
the exception and degraded gracefully, but the `requests` OpenTelemetry
auto-instrumentation still recorded an exception row on the span for every
dropped socket, polluting telemetry on each monitor poll tick.

## Root cause

The pooled k8s session's `urllib3.Retry` (`_build_k8s_retry`) used `read=0`.
urllib3 classifies a `RemoteDisconnected` / `ProtocolError` as a **read** error,
so `read=0` made the very first abort terminal — the request was never retried
even though an immediate reconnect would have succeeded.

## User-facing change

None visible. The warmup status card behaves identically (it already fell back
to "logs unavailable" on failure). The change is that a dropped-keepalive abort
is now transparently retried once instead of surfacing as a recorded exception.

## API / IaC diff summary

- `api/services/k8s/client.py` — `_build_k8s_retry()` now sets
  `read=_k8s_session_retry_total()` (was `0`). The single retry budget
  (`total=1`) bounds it, and `allowed_methods` stays GET/HEAD/OPTIONS only, so a
  mutating POST/PATCH/DELETE is still **never** replayed. HTTP status codes
  remain non-retried (`status=0`, `status_forcelist=()`).
- `api/tests/test_k8s_retry.py` — updated the pinned-config assertion
  (`read == _K8S_SESSION_RETRY_TOTAL`) and added two regression tests:
  - a `RemoteDisconnected`/`ProtocolError` on an idempotent GET is retried once
    then bounded (second abort raises `MaxRetryError`);
  - the same abort on a POST is **not** retried (urllib3 re-raises the original
    `ProtocolError`).

## Validation evidence

```
uv run pytest -q api/tests/test_k8s_retry.py     # 6 passed
uv run pytest -q api/tests                         # 3951 passed, 3 skipped
uv run ruff check api/services/k8s/client.py api/tests/test_k8s_retry.py  # clean
```
