---
title: Warmup pod-log ConnectionError no longer records App Insights exceptions
description: Suppress OpenTelemetry dependency exceptions for the best-effort warmup pod-log GET so transient AKS keep-alive aborts stop polluting App Insights.
tags:
  - operate
  - terminal
---

# Warmup pod-log ConnectionError suppression (#45)

## Motivation

`requests.exceptions.ConnectionError` from the AKS warmup pod-log fetch was the
single highest-volume App Insights exception (85 events / 7 days, ongoing). The
AKS API server drops pooled keep-alive sockets, which surfaces as
`ConnectionError(ProtocolError('Connection aborted.', RemoteDisconnected(...)))`
after the urllib3 retry budget is spent.

The application already caught the error and degraded to an empty log, but the
exception row still appeared: the `azure-monitor-opentelemetry` distro
auto-instruments `requests`/`urllib3` and records an exception event on the
client span *before* the app gets a chance to swallow it. App-level demotion
therefore could not remove the App Insights row.

## User-facing change

No UI change. The warmup status card keeps rendering logs, or a graceful empty
state when AKS drops the connection. Telemetry noise from the warmup pod-log GET
is eliminated.

## API / IaC diff summary

- `api/app/telemetry.py`: new `suppress_dependency_telemetry()` context manager
  that sets the OpenTelemetry instrumentation-suppression key for the enclosed
  block (no-op when OpenTelemetry is not installed). Scoped for best-effort,
  failure-tolerant, read-only calls only.
- `api/services/k8s/warmup_status.py`: `_warmup_pods_and_logs._fetch_log` now
  wraps the pod-log GET in `suppress_dependency_telemetry()` and demotes any
  failure to a one-line `LOGGER.warning` + empty-log fallback.

No infra change. No SAS/token surface change.

## Validation evidence

- `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py` — 9 passed,
  including the new `test_warmup_pod_log_connection_abort_degrades_without_exception`
  which raises a `ConnectionError(RemoteDisconnected)` on the pod-log GET and
  asserts (a) no exception propagates, (b) every pod degrades to no-log, and
  (c) the suppression context wraps every pod-log GET.
- `uv run pytest -q api/tests/test_telemetry_init.py` — 12 passed.
- `uv run ruff check` clean on the touched files.
