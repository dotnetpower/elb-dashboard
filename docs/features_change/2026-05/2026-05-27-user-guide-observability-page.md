# User Guide: Observability page

## Motivation

The dashboard already exposes two real-time observability surfaces (the Sidecar runtime band and the HTTP request inspector) plus an opt-in Application Insights pipeline configured from Settings → Telemetry, but there was no user-guide page that explained when to use which and how to turn the deeper pipeline on. Operators were either reading the architecture docs or guessing.

## User-facing change

- Added a new MkDocs page at [docs/user-guide/observability.md](../../user-guide/observability.md) walking through:
  - When to use the built-in dashboard surfaces vs Application Insights.
  - The Sidecar runtime band: cell meaning, refresh cadence, narrow-viewport behaviour.
  - The HTTP request inspector: latency scatter with SLA line, filter chips, 256-request buffer, and the bridge to App Insights for older data.
  - Settings → Telemetry: master toggle, deployment vs override connection string, Send test event / Apply to server sidecars / Clear server override actions, and the **Provision a resource** flow.
  - What lands in App Insights (`requests`, `dependencies`, `exceptions`, `customEvents`, `traces`) plus a one-shot KQL query that joins them on `customDimensions.request_id`.
  - Troubleshooting table and safe-screenshot practice.
- Embedded the three screenshots already staged under `docs/images/screenshots/observability/`:
  - `sidecar-runtime.png`
  - `sidecar-http-request.png`
  - `settings-telemetry.png`
- Registered the page in `mkdocs.yml` under **User Guide → Observability**, placed after **API Reference** and before **UI Preview**.

## API / IaC diff summary

Documentation-only change. No FastAPI route, OpenAPI spec, Bicep, or backend behaviour was modified.

- `docs/user-guide/observability.md` (new file).
- `mkdocs.yml` (one nav line added).

The page is a draft pending the additional Application Insights screenshots the maintainer will capture; image filenames in the page already match the existing `observability/` folder so adding more PNGs is a no-edit append later.

## Validation evidence

- `ls docs/images/screenshots/observability/` confirms the three referenced PNGs exist.
- `grep "Observability" mkdocs.yml` shows the new nav entry under User Guide.
- All external links (Microsoft Learn for Application Insights overview, JavaScript SDK, connection string) follow the docs-terminology rule (first meaningful use, Microsoft Learn for Azure concepts).
