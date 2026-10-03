---
title: Clarify persisted App Insights settings
description: Telemetry settings now distinguish the deployment connection from a device-local browser override so a blank input no longer looks like lost server configuration.
tags:
  - ui
  - operate
---

# Clarify persisted App Insights settings

## Motivation

The Telemetry settings page could show both `DEPLOYMENT CONNECTION` and `CONFIGURED` while leaving the connection string override input blank. The deployment setting remained present in [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview), but the input displayed only the browser-local override stored on that device. A fresh browser profile or cleared local storage therefore made a healthy server configuration look lost.

## User-Facing Change

When the deployment supplies the effective Application Insights connection, the browser source hint now includes the masked InstrumentationKey tail. The override field also explains that the deployment connection remains configured and that the field is blank only because this device has no browser override.

The effective deployment connection remains available to the settings UI even when browser telemetry is switched off. The full connection string is never rendered by this status message.

On narrow screens, the Settings section list now becomes a horizontal scroll rail so the active section retains the full viewport width instead of collapsing beside a fixed sidebar.

## API / IaC Diff Summary

- No API schema, persistence, authentication, RBAC, or IaC changed.
- The App Insights hook now retains the already-fetched effective connection in its context while the browser SDK is inactive.
- Telemetry display helpers and settings copy now distinguish deployment state from device-local override state.
- The Settings panel keeps its desktop sidebar and switches to a full-width content layout below 640 px.

## Validation Evidence

- Live status API reported a complete deployment connection without exposing its value during validation.
- The active Container App template contained `APPLICATIONINSIGHTS_CONNECTION_STRING` on api, worker, and beat.
- Focused telemetry display tests passed.
- Full frontend suite passed: 1,056 tests across 121 files.
- ESLint and strict MkDocs build passed.
- Frontend production build passed.
- Browser validation passed at 1204 x 761 and 390 x 844. The mobile content viewport reported equal client and scroll widths (`380px`), while the section rail remained intentionally horizontally scrollable.
