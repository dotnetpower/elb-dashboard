---
title: Service Bus three-state deployment guidance
description: Align deployment and Settings guidance with the existing Service Bus three-state override contract.
tags:
  - operate
  - ui
  - blast
---

# Service Bus three-state deployment guidance

## Motivation

The Service Bus runtime already treats `SERVICEBUS_ENABLED` as a three-state deployment override: explicit false is a kill switch, while unset/empty and true defer activation to the saved Settings config. `quick-deploy.sh` and the Settings Runtime hint still described the older two-gate model and incorrectly claimed that an unpinned environment value always hides Message Flow.

## User-facing change

- Deployment output warns only when `SERVICEBUS_ENABLED` is explicitly falsy and therefore forcing the integration off.
- The Settings Runtime hint now identifies the saved config as the activation control unless the deployment kill switch is explicitly false.
- Existing kill-switch remediation commands remain available when the override is active.

## API and infrastructure diff

No API, persisted config, Service Bus request body, MessageId, authentication mode, Container App environment default, or infrastructure resource changed. This change only corrects operator-facing text and adds source-contract tests for the three-state semantics.

## Validation

- `uv run pytest -q api/tests/test_service_bus_pref.py api/tests/test_settings_service_bus.py api/tests/test_control_plane_env.py`
- `cd web && npm test -- --run`
- `cd web && npm run build`
