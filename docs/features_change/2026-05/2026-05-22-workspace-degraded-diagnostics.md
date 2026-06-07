# 2026-05-22 — Workspace degraded-state diagnostics

## Motivation

The dashboard's monitor cards (AKS, Storage, ACR) silently rendered
"Ready" / "OK" status chips even when the backend's `_graceful` helper had
swallowed an ARM 401/403/404 and returned an empty `{"degraded": true,
"degraded_reason": "http_401"}` payload. Users wasted time investigating an
apparently-healthy dashboard whose underlying calls were actually failing.

The trigger that surfaced the bug: a developer with two Azure CLI profiles
(`az-demo` / `az-moonchoi`) had their `az login` session on tenant A but the
SPA's saved workspace config pointed at a subscription in tenant B. ARM
returned `InvalidAuthenticationTokenTenant`; the backend logged the error but
the SPA never showed anything actionable.

## User-facing change

* **Per-card status chips** now reflect the actual ARM result. When the
  backend returns a degraded payload, `MonitorCard` shows reason-specific
  labels (`Wrong tenant`, `No access`, `Not found`, `Network blocked`, …)
  with the full description in the tooltip, replacing the misleading
  `OK` / `Ready`.
* **`WorkspaceDiagnosticsBanner`** renders above the dashboard grid when the
  failure is workspace-wide: any `auth_wrong_tenant` on any card, or two or
  more cards reporting auth / `not_found`. A single forbidden leaf resource
  still defers to its own card.
* The banner has a **Reset workspace** button that clears the saved
  `SetupWizard` config from `localStorage` and re-runs the wizard, plus a
  per-reason dismiss button.

## Backend change

`api/routes/monitor/common.py` extracts the reason classifier into
`_classify_exception`. Stable `degraded_reason` codes are now part of the
SPA contract:

| Reason | When |
|---|---|
| `auth_wrong_tenant` | ARM 401 with `InvalidAuthenticationTokenTenant` / `AADSTS50020` markers |
| `unauthorized` | Other ARM 401 |
| `forbidden` | ARM 403 |
| `not_found` | ARM 404 / `ResourceNotFoundError` |
| `azure_error` | Other `AzureError` |
| `http_<status>` | Other HTTP errors |
| `<ClassName>` | Unknown exception (forward-compat) |

No new endpoint, no new permissions, no Bicep change.

## SPA change

* New shared helper `web/src/utils/monitorDegraded.ts` translating the
  taxonomy into UI labels + aggregating across cards.
* New helper `web/src/components/cards/cardStatusOverride.ts` mapping a
  degraded payload into a `MonitorCard.statusOverride` prop.
* `MonitorCard` accepts the new `statusOverride` prop; when present it wins
  over the regular `status` chip but the colour tones reuse the existing
  glass-theme chip palette (`gt-g/o/r/m`).
* `AcrCard` / `StorageCard` / `ClusterCard` opt in to the override.
* New `WorkspaceDiagnosticsBanner` component sits in `Dashboard.tsx`
  between the header and the grid. It re-uses the existing
  `monitoringApi.aks/storage/acr` queries (TanStack Query dedupes by
  `queryKey`, so no extra network calls) and consumes the aggregated
  diagnostics.

## Validation

```text
$ uv run pytest -q api/tests/test_monitor_graceful.py api/tests/test_route_contracts.py api/tests/test_smoke.py
96 passed in 5.39s

$ uv run ruff check api/routes/monitor/common.py api/tests/test_monitor_graceful.py
All checks passed!

$ cd web && npx vitest run src/utils/monitorDegraded.test.ts
Test Files  1 passed (1)
     Tests  13 passed (13)

$ cd web && npm run build
✓ built in 6.76s
```

Manual smoke (about to be re-verified live):

1. With `az` logged into tenant A and the SetupWizard pointing at a
   subscription in tenant B, the banner shows "Wrong Azure tenant for the
   selected subscription" with a Reset workspace button. Cards show "Wrong
   tenant" chip instead of "OK".
2. Reset workspace clears `localStorage` and re-runs the wizard.

## Follow-ups (separate PR)

* `SetupWizard` pre-validation: probe RG/ACR/Storage names against ARM
  before saving them, to prevent stale settings from being written in the
  first place.
* `/api/me` expose the list of subscriptions visible to the current
  credential so the wizard can show a dropdown instead of a free-form
  text field.
