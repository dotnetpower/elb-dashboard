---
title: Allow request-only Service Bus Settings
description: Service Bus Settings now preserves an explicitly blank completion topic so deployments can run queue-only ingestion without completion-event fan-out.
tags:
  - blast
  - ui
---

# Allow request-only Service Bus Settings

## Motivation

The Settings panel documented the completion topic as optional, but saving an
empty `completion_topic` re-expanded it to the default
`elastic-blast-completions` value. That made the request-only configuration
look accepted while still publishing completion events to the default topic.

## User-facing change

Operators can now clear **Completion topic** in Settings and save a real
request-only Service Bus configuration. The request queue remains required when
the integration is enabled; a blank completion topic only disables the optional
push/fan-out event channel.

## API / IaC diff summary

- `ServiceBusConfig.from_dict` now preserves an explicitly provided blank
  `completion_topic` while keeping the default for missing legacy rows.
- The Settings PUT/GET contract returns the blank value unchanged.
- No infrastructure change.

## Validation evidence

- `uv run pytest -q api/tests/test_service_bus_pref.py api/tests/test_settings_service_bus.py`