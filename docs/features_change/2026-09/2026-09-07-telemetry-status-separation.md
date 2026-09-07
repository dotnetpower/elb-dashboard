---
title: Separate browser and server telemetry status
description: Clarify the Telemetry settings so a browser-local off toggle no longer makes configured server-side Application Insights look disabled.
tags:
  - ui
  - user-guide
---

# Separate browser and server telemetry status

## Motivation

The Telemetry settings combined the browser SDK state and the deployed sidecar
connection source into one `Deployment · idle` badge. When browser telemetry was
off in local storage, the panel appeared to say that Application Insights was off
even while `api`, `worker`, and `beat` were configured and actively exporting.
The same condition also displayed **Provision a resource** for an existing,
configured deployment.

## User-facing change

- **Browser telemetry on this device** now states that the toggle affects only
  page views, browser requests, and browser errors from the current browser.
- **Browser connection source** reports connection availability without implying
  whether server collection is active.
- **Server sidecars** independently reports `Checking`, `Configured`, or
  `Not configured` from the authenticated deployment status endpoint.
- **Provision a resource** appears only after the status request confirms that no
  deployment connection exists and no valid browser override is present.

## API and implementation summary

- The existing `GET /api/settings/app-insights` response is unchanged.
- `AppInsightsProvider` now exposes its resolved `deployment_configured` state to
  the settings UI instead of inferring server state from the browser SDK.
- No Azure resource, RBAC, secret, connection string, or telemetry exporter
  configuration changes as part of this UI fix.

## Validation

- `npm test -- src/components/settings/sections/telemetryHelpers.test.tsx` - 5 passed.
- `npm test` - 1,004 passed.
- `npm run lint` - passed with zero warnings.
- `npm run build` - passed.
- `bash scripts/docs/build-mock-preview.sh` - passed.
- `uv run python scripts/docs/check_frontmatter.py` - passed.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` - passed.