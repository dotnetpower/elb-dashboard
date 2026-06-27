---
title: Service Bus client retry cap — bound dashboard-triggered calls to ~90s worst-case
description: Capped the Azure Service Bus client retry budget for every dashboard-triggered operation (peek, drain, entity counts, DLQ purge, external consumer drain). Default was retry_total=3 × retry_backoff_max=120 s = ~6 min internal retry; new defaults retry_total=3 × retry_backoff_max=30 s bound the worst case at ~90 s. Env-tunable so an operator can relax for a high-latency deployment without a redeploy.
tags:
  - operate
  - blast
---

# Service Bus client retry cap

## Motivation

The Azure `azure-servicebus` SDK has internal retry with generous defaults
(`retry_total=3`, `retry_backoff_factor=0.8`, `retry_backoff_max=120 s`) —
on a single transient broker hiccup the SDK can sleep up to ~6 minutes before
surfacing the error. Every `ServiceBusClient` and `ServiceBusAdministrationClient`
in `api/services/service_bus.py` plus the external consumer's drain client
in `api/services/service_bus_external_consumer.py` inherited those defaults,
which meant:

* A dashboard-triggered `peek` / `entity_counts` / DLQ list could tarpit a
  user-visible call for minutes when the broker is briefly unreachable.
* The external consumer's per-subscription `_drain_one` could stall the whole
  tick on a single bad subscription before its own backoff layer kicks in.

The orchestrator (beat, dashboard pollers) already re-fires these operations
on its own cadence, so the SDK's long internal retry was pure latency.

## Change

New module-private `_sb_client_kwargs()` in `api/services/service_bus.py`
reads env-tunable defaults and is applied to every `ServiceBusClient` /
`ServiceBusAdministrationClient` constructed by this codebase (4 sites in
`service_bus.py` + 1 site in `service_bus_external_consumer.py` via
`from api.services.service_bus import _sb_client_kwargs`).

New defaults:

* `retry_total=3` — unchanged from SDK default; one transient hiccup still
  rides through.
* `retry_backoff_max=30` — down from 120 s.

Worst-case retry budget per call is now ~30 + 30 + 30 = 90 s instead of
~120 + 120 + 120 = 360 s.

Env tunables:

* `SERVICEBUS_RETRY_TOTAL` (default `3`)
* `SERVICEBUS_RETRY_BACKOFF_MAX` (default `30`)

Either can be raised at the Container App without a code change if a future
deployment ever puts the api sidecar on a high-latency link.

## What this does *not* change

* `retry_backoff_factor` stays at the SDK default (`0.8`). The change is to
  the *ceiling*, not the slope.
* Production worker drain loops still keep running across hiccups — their
  own tick-level backoff layer (`_BACKOFF_START_SECONDS` in
  `service_bus_external_consumer.py`) is unchanged.
* The send/receive operation `max_wait_time` (5 s for peek, configurable for
  drain) is unchanged.

## Validation

* `uv run ruff check` → All checks passed.
* SB-specific suites (`test_service_bus_*.py`, `test_servicebus_tasks.py`,
  `test_settings_service_bus.py`) → 198 passed.
* Full `uv run pytest api/tests` → 4689 passed / 3 skipped in 2:18 (no
  regression).

## Risk

Low. Production retry behaviour is *tightened*, not loosened: a hiccup that
previously rode through the SDK's 360 s retry budget might now surface as a
caller-visible error after 90 s — but the orchestrator already retries every
such operation on its own cadence (beat scheduler, dashboard polling), so
the net effect on a transient broker is the same. The env tunables provide
a same-revision rollback if a customer environment turns out to need the old
defaults.

## Related

See [Unbounded Socket Timeouts — Audit & Lessons](../../research/unbounded-socket-timeouts.md)
for the broader context — this is one of the layer-2 (retry-loop) caps in
the same audit that traced the `pytest-xdist` "node down" mystery to
unbounded TCP connects.
