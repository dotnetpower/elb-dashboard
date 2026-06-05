---
title: Live Wall log SSE backs off when idle; monitor degradation is now metered
description: Adaptive poll backoff for the log stream plus an OTel counter for user-visible monitor degradation.
tags:
  - terminal
  - operate
---

# Live Wall log SSE backs off when idle; monitor degradation is now metered

Two independent observability/performance fixes surfaced during a state/monitor
performance critique.

## 1. Adaptive poll backoff for the Live Wall log stream

### Motivation

`/api/monitor/logs/{container}/events` tails a sidecar log file, which has no
event source, so the SSE generator polled `read_lines_since` every fixed `1 s`.
Every open-but-idle tab held that cadence forever — and each browser tab opens
up to six streams (one per sidecar) — so idle Live Walls woke the shared AnyIO
threadpool 6×/s for nothing. In Log-Analytics fallback mode the snapshot only
refreshes every ~5 s, so most of those wakeups returned no new lines at all.

### User-facing change

* While logs are flowing the stream stays at the `1 s` minimum (unchanged
  responsiveness). While idle it backs off geometrically (×1.5) toward a `5 s`
  cap and resets to the minimum the instant a new line arrives.
* Heartbeats and ticket auth are unchanged. Setting
  `LIVE_WALL_LOG_POLL_MAX_INTERVAL_SEC` to the minimum (≤ 1) restores the legacy
  fixed `1 s` cadence.

### Implementation

* New pure helpers in [api/routes/monitor/logs.py](../../../api/routes/monitor/logs.py):
  `_next_poll_interval(current, *, had_lines, max_interval)` and
  `_log_poll_max_interval_sec()` (env override, floored at the minimum, garbage →
  safe default). The SSE loop now advances its sleep via `_next_poll_interval`.

## 2. OTel counter for user-visible monitor degradation

### Motivation

`_graceful` (the monitor route degradation point) logged a `WARNING` and tagged
`degraded_reason`, but emitted no metric, so a systematic dashboard outage could
only be spotted by parsing logs. The cache layer already has
`elb_monitor_snapshot_refresh_failed`, but that fires on *every* loader failure —
including the ones a stale-cache fallback masks, where the browser still gets
valid data. There was no signal for "the browser actually received a degraded
payload".

### User-facing change

* New OpenTelemetry counter `elb_monitor_route_degraded`, labelled by `op`
  (route operation) and `reason` (classified degraded code), incremented exactly
  when `_graceful` serves a degraded body. Operators can now alert on real
  user-visible degradation per card. `cache-counter ≥ route-counter`, and the gap
  is the degradation the stale cache absorbed.
* No response-shape change; the counter is a side effect. A broken meter never
  turns a graceful degrade into a 500.

### Implementation

* [api/routes/monitor/common.py](../../../api/routes/monitor/common.py): lazy
  `_get_degraded_counter()` mirroring the `monitor_cache` pattern (null-safe when
  OTel is not initialised), a `_reset_degraded_counter()` test hook, and a
  guarded `.add(1, {"op", "reason"})` in `_graceful`.

## API / IaC diff summary

* `api/routes/monitor/logs.py` — adaptive poll backoff helpers + loop.
* `api/routes/monitor/common.py` — route-degraded OTel counter.
* No infra change. No new dependency (OTel + AnyIO already present).

## Validation evidence

* New tests:
  * `test_next_poll_interval_resets_on_lines_and_backs_off_when_idle`,
    `test_log_poll_max_interval_env_override`
    ([api/tests/test_sidecar_logs.py](../../../api/tests/test_sidecar_logs.py)).
  * `test_graceful_increments_degraded_counter`,
    `test_graceful_counter_failure_never_breaks_degrade`
    ([api/tests/test_monitor_graceful.py](../../../api/tests/test_monitor_graceful.py)).
* `uv run ruff check` on all four files — clean.
* Focused: `test_sidecar_logs.py test_monitor_graceful.py test_monitor_cache.py`
  — 49 passed.
* Full backend sweep: `uv run pytest -q api/tests` — 2778 passed, 3 skipped (one
  unrelated `test_terminal_exec` truncation test flaked under parallel load and
  passes in isolation; not in the changed modules' dependency graph).
